#!/usr/bin/env python3
"""Проверка виджета перед sendWidget: размеры HTML/состояния по Bot API 10.4.

Сервер считает zlib level 9 (RFC 1950, PHP gzcompress) и отказывает с
INPUT_MEDIA_WIDGET_TOO_LARGE, если >4096 байт. Лучше узнать это на билде.

    python scripts/widget_budget.py widget.html --chat-data '{"v":1,"p":0}' \
        --query n --answer ok --height 180
"""
from __future__ import annotations

import argparse
import json
import sys
import zlib

MAX_HTML_RAW = 32_768
MAX_HTML_ZLIB = 4_096          # рекомендуемый запас в коде: держаться < 3900
MAX_STATE = 1_024             # chat_data / public_data, байты UTF-8
MAX_QUERY = 64                # TelegramWidget.request(...)
MAX_ANSWER = 1_024            # answerWidgetQuery.data
MAX_CAPTION = 1_024           # символов после парсинга entities
HEIGHT_RANGE = (50, 600)
WIDTH_MIN = 200


def _byte_len(value: str) -> int:
    return len(value.encode("utf-8"))


def check(html: str, *, chat_data: str | None, public_data: str | None,
          query: str | None, answer: str | None, caption: str | None,
          height: int | None, width: int | None, strict_zlib: int) -> list[str]:
    errors: list[str] = []
    raw = _byte_len(html)

    if not html:
        errors.append("HTML пустой (нужно 1..32768 байт)")
    if raw > MAX_HTML_RAW:
        errors.append(f"HTML {raw} Б > {MAX_HTML_RAW} Б")
    if "\x00" in html:
        errors.append("HTML содержит NUL (U+0000)")

    packed = len(zlib.compress(html.encode("utf-8"), 9))
    if packed > MAX_HTML_ZLIB:
        errors.append(f"zlib-9 = {packed} Б > {MAX_HTML_ZLIB} Б (лимит сервера)")
    elif packed > strict_zlib:
        errors.append(f"zlib-9 = {packed} Б > запаса {strict_zlib} Б; оставь место под фичи")

    for name, value in (("chat_data", chat_data), ("public_data", public_data)):
        if value is None:
            continue
        size = _byte_len(value)
        if size > MAX_STATE:
            errors.append(f"{name} = {size} Б > {MAX_STATE} Б")
        if "\x00" in value:
            errors.append(f"{name} содержит NUL")
        try:
            json.loads(value) if value.strip().startswith("{") else None
        except json.JSONDecodeError as exc:
            errors.append(f"{name} не валидный JSON: {exc}")

    if query is not None:
        size = _byte_len(query)
        if not 1 <= size <= MAX_QUERY:
            errors.append(f"request = {size} Б, нужно 1..{MAX_QUERY} Б")
    if answer is not None and _byte_len(answer) > MAX_ANSWER:
        errors.append(f"answer > {MAX_ANSWER} Б")
    if caption is not None and len(caption) > MAX_CAPTION:
        errors.append(f"caption > {MAX_CAPTION} символов")
    if height is not None and not HEIGHT_RANGE[0] <= height <= HEIGHT_RANGE[1]:
        errors.append(f"height={height}, клиент прижмёт к {HEIGHT_RANGE}")
    if width is not None and width < WIDTH_MIN:
        errors.append(f"width={width}, клиент прижмёт к >= {WIDTH_MIN}")

    print(f"HTML: {raw} Б raw / {packed} Б zlib-9 "
          f"(лимит {MAX_HTML_ZLIB} Б, запас {strict_zlib} Б)")
    for name, value in (("chat_data", chat_data), ("public_data", public_data)):
        if value is not None:
            print(f"{name}: {_byte_len(value)} Б / {MAX_STATE} Б")
    return errors


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("html", help="путь к HTML виджета или '-' для stdin")
    ap.add_argument("--chat-data")
    ap.add_argument("--public-data")
    ap.add_argument("--query", help="пример строки TelegramWidget.request()")
    ap.add_argument("--answer", help="пример answerWidgetQuery.data")
    ap.add_argument("--caption")
    ap.add_argument("--height", type=int)
    ap.add_argument("--width", type=int)
    ap.add_argument("--strict-zlib", type=int, default=3_900,
                    help="жёсткий внутренний порог (по умолчанию 3900)")
    args = ap.parse_args()

    html = sys.stdin.read() if args.html == "-" else open(args.html, encoding="utf-8").read()
    errors = check(html, chat_data=args.chat_data, public_data=args.public_data,
                   query=args.query, answer=args.answer, caption=args.caption,
                   height=args.height, width=args.width, strict_zlib=args.strict_zlib)
    for err in errors:
        print(f"FAIL: {err}", file=sys.stderr)
    if errors:
        return 1
    print("OK: виджет в бюджете Bot API 10.4")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
