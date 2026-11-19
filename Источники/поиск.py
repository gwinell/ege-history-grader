"""Локальный поиск по параграфам государственных учебников."""

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent
LOCAL = ROOT / "локальные-источники.json"
BUDGET = 1800
MAX_BUDGET = 3600


def load_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def normalize(value):
    return value.casefold().replace("ё", "е")


def terms(value):
    return {word if len(word) < 6 else word[:-2]
            for word in re.findall(r"[а-яa-z0-9]+", normalize(value))
            if len(word) >= 3 or word.isdigit()}


def paragraphs(value):
    return re.sub(r"[\s§]+", "", value).replace("-", "–").replace("—", "–")


def chapter_numbers(value):
    bounds = [int(part) for part in value.split("–")]
    return range(bounds[0], bounds[-1] + 1)


def sources():
    catalog = load_json(ROOT / "каталог.json")
    files = load_json(LOCAL) if LOCAL.exists() else {}
    chapters = {}
    for book_id, book in catalog["books"].items():
        for section, title in book["chapters"].items():
            source_id = f"{book_id}:{section}"
            chapters[source_id] = {
                "course": book["course"],
                "book": book.get("book", book["course"]),
                "grade": book["grade"],
                "paragraph": section,
                "title": title,
            }
            if "edition" in book:
                chapters[source_id]["edition"] = book["edition"]
    for source_id, sample in catalog["samples"].items():
        chapters[source_id].update(sample)
    for source_id, entry in files.items():
        chapters[source_id] = {**chapters.get(source_id, {}), **entry}
    return catalog, chapters, files


def describe(source_id, entry):
    description = {"id": source_id}
    for field in ("course", "book", "grade", "edition", "paragraph", "title", "isbn",
                  "kind", "author", "reference"):
        if entry.get(field) is not None:
            description[field] = entry[field]
    description["text_available"] = bool(
        entry.get("text") or (entry.get("file") and Path(entry["file"]).is_file())
    )
    return description


def find(args, catalog, chapters):
    if args.demo_task is not None:
        ids = catalog["demo"][str(args.demo_task)]
        if not ids:
            return {"status": "use_task_source", "source": catalog["source"]}
        return {"status": "ok", "source": catalog["source"],
                "chapters": [describe(source_id, chapters[source_id]) for source_id in ids]}

    words = terms(args.topic or "")
    requested = set(chapter_numbers(args.paragraph)) if args.paragraph else set()
    candidates = []
    for source_id, chapter in chapters.items():
        if args.grade and chapter["grade"] != args.grade:
            continue
        if args.course and normalize(chapter["course"]) != normalize(args.course):
            continue
        if requested and not requested.issubset(chapter_numbers(chapter["paragraph"])):
            continue
        title = normalize(chapter["title"])
        hints = normalize(catalog["keywords"].get(source_id, "") + " " + chapter.get("keywords", ""))
        score = sum(2 if word in title else int(word in hints) for word in words)
        if words and score == 0:
            continue
        entry = describe(source_id, chapter)
        candidates.append((score, entry))
    candidates.sort(key=lambda item: (-item[0], not item[1]["text_available"], item[1]["id"]))
    matches = [entry for _, entry in candidates[:args.limit]]
    return {"status": "ok" if matches else "no_match", "chapters": matches}


def read_pages(entry, page):
    if entry.get("text"):
        if page is not None:
            raise ValueError("В справке указаны печатные страницы. Параметр --page применяется к PDF.")
        return [(None, entry["text"])]
    path = Path(entry["file"])
    if path.suffix.lower() != ".pdf":
        if page is not None:
            raise ValueError("Для текстового фрагмента номер страницы задаётся при подключении.")
        return [(entry.get("label_pages"), path.read_text(encoding="utf-8"))]
    first, last = entry["pages"]
    if page is not None:
        if not first <= page <= last:
            raise ValueError("Страница за пределами подключённого параграфа.")
        first = last = page
    output = subprocess.run(
        ["pdftotext", "-enc", "UTF-8", "-f", str(first), "-l", str(last), str(path), "-"],
        check=True, capture_output=True, text=True, encoding="utf-8",
    ).stdout
    return [(first + index, body) for index, body in enumerate(output.split("\f")[:last - first + 1])]


def read(args, chapters):
    entry = chapters.get(args.id)
    if entry is None:
        return {"status": "unknown_id", "id": args.id}
    source = describe(args.id, entry)
    if not source["text_available"]:
        return {"status": "text_unavailable", "source": source}
    words = terms(args.query or "")
    matches = []
    for page, body in read_pages(entry, args.page):
        body = " ".join(body.split())
        if not body:
            continue
        normalized = normalize(body)
        hits = {word: normalized.find(word) for word in words}
        positions = [position for position in hits.values() if position >= 0]
        matches.append((len(positions), page, body, min(positions, default=0)))
    if not matches:
        return {"status": "no_text_layer", "source": source}
    matches.sort(key=lambda item: -item[0])
    supported = [item for item in matches if item[0] or not words]
    if len(supported) < args.match:
        return {"status": "no_match", "source": source, "matching_pages": len(supported)}
    _, page, body, position = supported[args.match - 1]
    start = min(max(0, position - args.budget // 4), max(0, len(body) - args.budget))
    end = start + args.budget
    return {
        "status": "ok",
        "source": source,
        "pdf_page": page if entry.get("pages") else None,
        "printed_pages": entry.get("label_pages"),
        "matching_pages": len(supported),
        "excerpt": body[start:end],
        "truncated_before": start > 0,
        "truncated_after": end < len(body),
    }


def add(args, chapters, files):
    path = Path(args.file).expanduser().resolve(strict=True)
    if not path.is_file() or path.suffix.lower() not in {".pdf", ".txt", ".md"}:
        raise ValueError("Укажите файл PDF, TXT или Markdown.")
    if path.suffix.lower() == ".pdf" and not args.pages:
        raise ValueError("Для PDF укажите первую и последнюю физические страницы параграфа.")
    if args.pages and (path.suffix.lower() != ".pdf" or not 1 <= args.pages[0] <= args.pages[1]):
        raise ValueError("Диапазон страниц применяется к PDF и начинается с 1.")
    entry = dict(chapters.get(args.id, {}))
    known_edition = entry.get("edition") if entry.get("kind") != "summary" else None
    if known_edition and args.edition and known_edition != args.edition:
        raise ValueError(f"У источника {args.id} указан год {known_edition}. Для другого издания задайте отдельный ID.")
    if not entry:
        if not all((args.course, args.grade, args.paragraph, args.title)):
            raise ValueError("Для нового ID укажите курс, класс, параграф и название.")
        entry = {"course": args.course, "book": args.book or args.course,
                 "grade": args.grade, "paragraph": args.paragraph, "title": args.title}
    entry["file"] = str(path)
    for field in ("pages", "label_pages", "isbn", "text", "reference", "author"):
        # Явное пустое значение заменяет сведения о встроенной справке.
        entry[field] = None
    if entry.get("kind") == "summary":
        entry["edition"] = None
    entry["kind"] = "textbook_excerpt"
    for field in ("edition", "pages", "label_pages", "isbn", "keywords", "author"):
        if getattr(args, field) is not None:
            entry[field] = getattr(args, field)
    files[args.id] = entry
    temporary = LOCAL.with_suffix(".tmp")
    temporary.write_text(json.dumps(files, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(LOCAL)
    return {"status": "ok", "source": describe(args.id, entry)}


def paragraph_range(value):
    value = paragraphs(value)
    if not re.fullmatch(r"[1-9]\d*(?:–[1-9]\d*)?", value) or not chapter_numbers(value):
        raise argparse.ArgumentTypeError("Укажите номер или диапазон параграфов, например 43 или 5–6.")
    return value


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    lookup = commands.add_parser("find", help="Вернуть только сведения о подходящих параграфах")
    lookup.add_argument("--demo-task", type=int, choices=range(1, 23))
    lookup.add_argument("--topic")
    lookup.add_argument("--grade", type=int)
    lookup.add_argument("--course")
    lookup.add_argument("--paragraph", type=paragraph_range)
    lookup.add_argument("--limit", type=int, choices=range(1, 6), default=3)
    excerpt = commands.add_parser("read", help="Прочитать фрагмент одного параграфа")
    excerpt.add_argument("id")
    excerpt.add_argument("--query")
    excerpt.add_argument("--page", type=int, help="Физическая страница из подключённого диапазона")
    excerpt.add_argument("--match", type=int, default=1, help="Порядковый номер подходящей страницы")
    excerpt.add_argument("--budget", type=int, default=BUDGET, help=f"Лимит символов до {MAX_BUDGET}")
    register = commands.add_parser("add", help="Подключить локальный файл и страницы параграфа")
    register.add_argument("id")
    register.add_argument("file")
    register.add_argument("--pages", type=int, nargs=2, metavar=("FIRST", "LAST"))
    register.add_argument("--label-pages", help="Печатные номера страниц в книге")
    register.add_argument("--edition", type=int, help="Год издания")
    register.add_argument("--isbn")
    register.add_argument("--author")
    register.add_argument("--keywords")
    register.add_argument("--course", choices=("История России", "Всеобщая история"))
    register.add_argument("--grade", type=int, choices=range(5, 12))
    register.add_argument("--paragraph", type=paragraph_range)
    register.add_argument("--title")
    register.add_argument("--book")
    args = parser.parse_args()
    if args.command == "read" and (not 1 <= args.budget <= MAX_BUDGET or args.match < 1):
        parser.error(f"Укажите бюджет от 1 до {MAX_BUDGET} и номер совпадения от 1.")
    try:
        catalog, chapters, files = sources()
        if args.command == "find":
            result = find(args, catalog, chapters)
        elif args.command == "read":
            result = read(args, chapters)
        else:
            result = add(args, chapters, files)
    except FileNotFoundError as error:
        result = {"status": "file_or_tool_missing", "detail": str(error)}
    except subprocess.CalledProcessError as error:
        result = {"status": "pdf_read_error", "detail": error.stderr.strip()}
    except (OSError, ValueError, KeyError) as error:
        result = {"status": "error", "detail": str(error)}
    print(json.dumps(result, ensure_ascii=False))
    return int(result["status"] in {"file_or_tool_missing", "pdf_read_error", "error"})


if __name__ == "__main__":
    sys.exit(main())
