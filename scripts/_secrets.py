"""公開リポジトリで運用するための、認証情報の取り扱いをまとめたモジュール。

このリポジトリはpublicなので、GitHub Actionsの実行ログもWeb上で誰でも読める。
GitHub側でもSecretsに登録した値は自動でマスクされるが、次の場合はすり抜ける:

  - URLエンコードされるなどして、登録した文字列と完全一致しない形でログに出た
  - Secrets以外の経路(ローカル実行や .env)で渡した値だった
  - 未捕捉の例外でトレースバックが出て、その中にリクエストURLが載った

そのため「外から受け取った文字列をそのままログに出さない」ことをコード側でも徹底する。
例外メッセージ・APIレスポンス・外部コマンドの出力をprintする箇所は、必ず redact() を通すこと。
"""

import os
import re
import sys
import traceback
from urllib.parse import quote

# 値そのものをマスク対象にする環境変数。ここに挙げた変数の中身は、
# どんな文字列の中に現れても伏せ字にする。
SECRET_ENV_VARS = (
    "THREADS_ACCESS_TOKEN",
    "GEMINI_API_KEY",
    "GITHUB_TOKEN",
)

MASK = "***"

# 実際の値が手元に無い場合でも拾えるように、キー名でもマスクする。
_KEY_NAMES = r"access_token|client_secret|api_key|apikey|key|token|password"

_PATTERNS = (
    # URLのクエリやフォーム本体: ?key=xxx / &access_token=xxx
    (re.compile(rf"((?:{_KEY_NAMES})=)[^&\s\"'<>]+", re.I), rf"\1{MASK}"),
    # JSON: "access_token": "xxx"
    (re.compile(rf"([\"'](?:{_KEY_NAMES})[\"']\s*:\s*[\"'])[^\"']*", re.I), rf"\1{MASK}"),
    # HTTPヘッダ: Authorization: Bearer xxx
    (re.compile(r"((?:Bearer|Basic)\s+)[A-Za-z0-9._\-=+/]+", re.I), rf"\1{MASK}"),
    # gitのリモートURLに埋め込まれた認証情報: https://user:pass@github.com/...
    (re.compile(r"(https?://)[^/\s:@]+:[^/\s@]+@"), rf"\1{MASK}:{MASK}@"),
)


def _literal_secrets() -> list[str]:
    """環境変数に入っている実際の秘密の値を、長い順に返す。

    8文字未満は対象外にしている。短い値まで含めると、たまたま一致しただけの
    無関係な文字列まで潰してしまいログが読めなくなるため。
    長い順に置換するのは、ある値が別の値の一部だったときに取りこぼさないため。
    """
    values: set[str] = set()
    for name in SECRET_ENV_VARS:
        value = os.environ.get(name, "").strip()
        if len(value) >= 8:
            values.add(value)
            # URLエンコードされた形でログに出ることがあるので、その形も潰す
            encoded = quote(value, safe="")
            if encoded != value:
                values.add(encoded)
    return sorted(values, key=len, reverse=True)


def redact(text: object) -> str:
    """ログに出す前に、認証情報らしき部分を伏せ字にする。

    「実際の値そのもの」と「キー名からの推測」の二段構えにしている。
    前者だけだと値が加工されていた場合に漏れ、後者だけだと想定外の
    書式で出てきた場合に漏れるため、両方かけている。
    """
    result = str(text)
    for secret in _literal_secrets():
        result = result.replace(secret, MASK)
    for pattern, replacement in _PATTERNS:
        result = pattern.sub(replacement, result)
    return result


def run_safely(main_func, tag: str) -> None:
    """main() を包んで、未捕捉の例外でトレースバックが生で出るのを防ぐ。

    トレースバックには例外メッセージがそのまま載る。requests の例外は
    リクエストURLを含むため、クエリに認証情報が乗っていると公開ログに
    残ってしまう。ここで一度受け止め、redact() を通してから出力する。

    終了コードは1のままにしてあるので、失敗はGitHub Actions上でも赤く出る。
    """
    try:
        main_func()
    except SystemExit:
        raise
    except BaseException:
        print(f"[{tag}] 処理中にエラーが発生しました:", file=sys.stderr)
        print(redact(traceback.format_exc()), file=sys.stderr)
        sys.exit(1)
