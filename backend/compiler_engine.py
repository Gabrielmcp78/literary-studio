"""Bridge Literary Studio projects into the Fiction Git Studio typesetter.

Literary Studio stores the active manuscript as project-scoped scene HTML. The
authoritative formatter lives in the sibling Fiction Git Studio repository: it
parses structured Markdown, applies a named format profile, emits provenance,
and compiles through a real LaTeX engine. This module adapts the Studio scene
store to that contract without reading or modifying a manuscript checkout.
"""

from __future__ import annotations

import os
import re
import shutil
import sys
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

# Ensure standard TeX binaries and Homebrew paths are in PATH on macOS
for _bin_dir in (
    "/Library/TeX/texbin",
    "/usr/local/bin",
    "/opt/homebrew/bin",
    "/opt/homebrew/sbin",
):
    if os.path.isdir(_bin_dir) and _bin_dir not in os.environ.get("PATH", "").split(":"):
        os.environ["PATH"] = f"{_bin_dir}:{os.environ.get('PATH', '')}"


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_FICTION_GIT_ROOT = PROJECT_ROOT.parent / "fiction-git-system"
DEFAULT_PROFILE_ID = "manuscript_submission"
DEFAULT_AUTHOR = "Gabriel McPherson"


class FormatterUnavailable(RuntimeError):
    """Raised when the authoritative Fiction Git Studio formatter is absent."""


class _HtmlToMarkdown(HTMLParser):
    """Convert the editor's small HTML vocabulary back to formatter Markdown."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.link_href: str | None = None
        self.in_blockquote: int = 0

    def _blank(self) -> None:
        text = "".join(self.parts).rstrip()
        if not text:
            self.parts = []
        elif re.search(r"<!--\s*literary-style:[^>]+-->$", text, flags=re.IGNORECASE):
            # Keep the directive attached to the block it annotates. A blank
            # line here would make the importer treat the directive as a
            # standalone paragraph and lose the hierarchy on reload.
            self.parts = [text, "\n"]
        else:
            self.parts = [text, "\n\n"]

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        attr_map = dict(attrs)
        semantic_style = (attr_map.get("data-semantic-style") or "").strip()
        style_locked = (attr_map.get("data-style-locked") or "").strip().lower() == "true"
        if semantic_style and tag in {"p", "div", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote"}:
            # Keep semantic hierarchy in Markdown without leaking editor HTML
            # into the source file. The importer consumes this directive and
            # restores the data attributes on the next load.
            self._blank()
            lock_suffix = " locked" if style_locked else ""
            self.parts.append(f"<!-- literary-style: {semantic_style}{lock_suffix} -->\n")
        if tag == "blockquote":
            self._blank()
            self.in_blockquote += 1
        elif tag in {"p", "div", "h1", "h2", "h3", "h4", "h5", "h6", "ul", "ol"}:
            if self.in_blockquote:
                text = "".join(self.parts).rstrip()
                if text:
                    self.parts = [text, "\n\n> "]
                else:
                    self.parts = ["> "]
            else:
                self._blank()
        if tag in {"h1", "h2", "h3", "h4", "h5", "h6"}:
            self.parts.append("#" * int(tag[1]) + " ")
        elif tag in {"strong", "b"}:
            self.parts.append("**")
        elif tag in {"em", "i"}:
            self.parts.append("*")
        elif tag in {"s", "del"}:
            self.parts.append("~~")
        elif tag == "code":
            self.parts.append("`")
        elif tag == "li":
            self._blank()
            self.parts.append("- ")
        elif tag == "br":
            self.parts.append("\n> " if self.in_blockquote else "\n")
        elif tag == "hr":
            self._blank()
            self.parts.append("***")
            self._blank()
        elif tag == "a":
            href = dict(attrs).get("href")
            self.link_href = href or ""
            self.parts.append("[")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag == "blockquote":
            self.in_blockquote = max(0, self.in_blockquote - 1)
            self._blank()
        elif tag in {"strong", "b"}:
            self.parts.append("**")
        elif tag in {"em", "i"}:
            self.parts.append("*")
        elif tag in {"s", "del"}:
            self.parts.append("~~")
        elif tag == "code":
            self.parts.append("`")
        elif tag == "a":
            self.parts.append(f"]({self.link_href})" if self.link_href else "]")
            self.link_href = None
        elif tag in {"p", "div", "h1", "h2", "h3", "h4", "h5", "h6", "li"}:
            if not self.in_blockquote:
                self._blank()

    def handle_data(self, data: str) -> None:
        self.parts.append(data)

    def markdown(self) -> str:
        value = "".join(self.parts)
        value = re.sub(r"[ \t]+\n", "\n", value)
        value = re.sub(r"\n{3,}", "\n\n", value)
        return value.strip()


def html_to_markdown(value: str) -> str:
    parser = _HtmlToMarkdown()
    parser.feed(value or "")
    parser.close()
    return parser.markdown()


def _fiction_git_root() -> Path:
    configured = os.environ.get("FICTION_GIT_SYSTEM_ROOT", "").strip()
    return Path(configured).expanduser().resolve() if configured else DEFAULT_FICTION_GIT_ROOT


def _load_typesetter() -> tuple[Any, Any, Any]:
    root = _fiction_git_root()
    if not root.exists():
        raise FormatterUnavailable(
            f"Authoritative formatter repository not found at {root}. "
            "Set FICTION_GIT_SYSTEM_ROOT to the Fiction Git Studio checkout."
        )
    root_text = str(root)
    # The Studio's registry is the semantic contract for both the editor and
    # the sibling print formatter. The formatter has a standalone fallback,
    # but project exports always point it at this exact registry.
    registry_path = PROJECT_ROOT / "frontend" / "data" / "semanticStyles.json"
    if registry_path.is_file():
        os.environ.setdefault("LITERARY_STYLE_REGISTRY", str(registry_path))
    if root_text not in sys.path:
        sys.path.insert(0, root_text)
    try:
        from fiction_git_system.tools.typeset_exporter import (  # type: ignore[import-not-found]
            compile_latex_pdf,
            export_latex,
            list_format_profiles,
        )
    except Exception as exc:  # pragma: no cover - depends on external checkout
        raise FormatterUnavailable(f"Unable to load Fiction Git Studio typesetter: {exc}") from exc
    return export_latex, compile_latex_pdf, list_format_profiles


DEFAULT_FORMAT_PROFILES: list[dict[str, str]] = [
    {
        "id": "manuscript_submission",
        "label": "Manuscript Submission (Standard Shunn)",
        "target": "latex",
        "description": "Letter-size editorial submission profile with one-inch margins, simple headers, and double-spaced prose.",
    },
    {
        "id": "trade_paperback_prh",
        "label": "Trade Paperback Literary",
        "target": "latex",
        "description": "Six-by-nine literary trade paperback profile with house-style title pages, bottom outside folios, drop caps, scene rules, and font fallback.",
    },
]


def list_format_profiles() -> list[dict[str, str]]:
    """Expose the authoritative LaTeX profiles to the Studio UI."""
    try:
        _, _, list_profiles = _load_typesetter()
        return [profile for profile in list_profiles() if profile.get("target") == "latex"]
    except FormatterUnavailable:
        return DEFAULT_FORMAT_PROFILES


def _safe_filename(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.casefold()).strip("-")
    return slug or "manuscript"


def _frontmatter_value(value: str) -> str:
    escaped = str(value or "").replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _display_chapter_title(title: str | None) -> str:
    """Remove Studio's path prefix and duplicate chapter label for book layout."""
    clean = str(title or "Unstructured material").strip()
    clean = re.sub(r"^act[-_ ]\d+\s*/\s*", "", clean, flags=re.IGNORECASE)
    clean = re.sub(r"^(?:chapter|ch)[-_ ]?\d+\s*[:|—-]\s*", "", clean, flags=re.IGNORECASE)
    clean = re.sub(r"^\d+\s*[|—:-]\s*", "", clean)
    return clean or "Unstructured material"


def _display_part_title(scene: Mapping[str, Any], fallback: int | None = None) -> str:
    title = str(scene.get("part_title") or "").strip()
    number = scene.get("part_number")
    if title:
        return title
    try:
        return f"Part {int(number)}"
    except (TypeError, ValueError):
        return f"Part {fallback}" if fallback is not None else ""


def _chapter_sort_key(scene: Mapping[str, Any], fallback: int) -> tuple[int, int, str]:
    chapter_number = scene.get("chapter_number")
    try:
        number = int(chapter_number)
    except (TypeError, ValueError):
        number = 0 if any(term in str(scene.get("chapter_title", "")).casefold() for term in ("epigraph", "prologue")) else 9999
    title = str(scene.get("chapter_title") or "")
    act_match = re.search(r"act[-_ ](\d+)", title, flags=re.IGNORECASE)
    act = int(act_match.group(1)) if act_match else fallback
    return act, number, title


def _exportable_scenes(scenes: Iterable[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    """Keep compiler exports limited to manuscript prose, never runtime/support material."""
    exportable: list[Mapping[str, Any]] = []
    for scene in scenes:
        source_path = str(scene.get("source_path") or "").casefold()
        if any(
            excluded in source_path
            for excluded in (
                "worldbook",
                "editor-passes",
                "roundtable",
                "scaffolding",
                "assessment",
                "scratch",
            )
        ):
            continue
        exportable.append(scene)
    return exportable


def select_export_scenes(
    scenes: Iterable[Mapping[str, Any]],
    scene_numbers: Sequence[int] | None = None,
    chapter_numbers: Sequence[int] | None = None,
    part_numbers: Sequence[int] | None = None,
) -> list[Mapping[str, Any]]:
    """Apply the hierarchical export selection used by the compiler UI.

    An empty selection means the complete manuscript. When any selection is
    supplied, a part includes every scene in that part, a chapter includes
    every scene in that chapter, and explicit scene numbers include only those
    scenes. This keeps the API deterministic while allowing the UI to expose
    one part/chapter/scene tree.
    """
    exportable = _exportable_scenes(scenes)
    selected_scenes = {int(value) for value in (scene_numbers or [])}
    selected_chapters = {int(value) for value in (chapter_numbers or [])}
    selected_parts = {int(value) for value in (part_numbers or [])}
    if not selected_scenes and not selected_chapters and not selected_parts:
        return exportable

    selected: list[Mapping[str, Any]] = []
    for scene in exportable:
        try:
            scene_number = int(scene.get("scene_number"))
        except (TypeError, ValueError):
            scene_number = None
        try:
            chapter_number = int(scene.get("chapter_number"))
        except (TypeError, ValueError):
            chapter_number = None
        try:
            part_number = int(scene.get("part_number"))
        except (TypeError, ValueError):
            part_number = None
        if (
            (scene_number is not None and scene_number in selected_scenes)
            or (chapter_number is not None and chapter_number in selected_chapters)
            or (part_number is not None and part_number in selected_parts)
        ):
            selected.append(scene)
    return selected


def _ordered_export_scenes(scenes: Iterable[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    exportable = _exportable_scenes(scenes)

    def key(scene: Mapping[str, Any], fallback: int) -> tuple[int, tuple[int, int, str], int, int]:
        try:
            part_number = int(scene.get("part_number"))
        except (TypeError, ValueError):
            part_number = 9999
        try:
            scene_in_chapter = int(scene.get("scene_number_in_chapter"))
        except (TypeError, ValueError):
            scene_in_chapter = 9999
        try:
            scene_number = int(scene.get("scene_number"))
        except (TypeError, ValueError):
            scene_number = fallback
        return part_number, _chapter_sort_key(scene, fallback), scene_in_chapter, scene_number

    return [
        scene
        for _, scene in sorted(
            enumerate(exportable),
            key=lambda item: key(item[1], item[0]),
        )
    ]


def _provenance_frontmatter(provenance: Mapping[str, Any] | None) -> list[str]:
    if not provenance:
        return []
    return [
        f"generated_at: {_frontmatter_value(str(provenance.get('generated_at') or ''))}",
        f"git_branch: {_frontmatter_value(str(provenance.get('branch') or 'detached'))}",
        f"git_commit: {_frontmatter_value(str(provenance.get('commit_sha') or 'unknown'))}",
        "page_numbering: \"full-book\"",
    ]


def _scene_heading_text(scene: Mapping[str, Any]) -> str:
    """Return the formatter's structured scene heading for one scene.

    Preserves chapter-scene hierarchy (e.g. 6.1) and compound literary headers
    (SUBJECT | Location | Timeline) for exact alignment with the Typeset Exporter
    and SCENE_HEADING_RE parser.
    """
    label = str(scene.get("title") or "").strip()
    if not label or re.fullmatch(r"scene\s+\d+", label, flags=re.IGNORECASE):
        return ""

    chapter_num = scene.get("chapter_number")
    ordinal = scene.get("scene_number_in_chapter")

    clean_label = label.replace("--", "—")
    clean_label = re.sub(r"^\d+(?:\.\d+)?\s*[|—–:-]?\s*", "", clean_label).strip()

    try:
        ord_int = int(ordinal)
        if chapter_num not in (None, "", 0):
            prefix = f"{int(chapter_num)}.{ord_int}"
        else:
            prefix = f"{ord_int}"
    except (TypeError, ValueError):
        prefix = ""

    if prefix and clean_label:
        return f"{prefix}  {clean_label}"
    if prefix:
        return prefix
    return clean_label


def render_markdown_export(
    scenes: Iterable[Mapping[str, Any]],
    title: str,
    author: str,
    provenance: Mapping[str, Any] | None = None,
) -> str:
    """Render the selected project scenes as a standalone Markdown manuscript."""
    ordered = _ordered_export_scenes(scenes)
    if not ordered:
        raise ValueError("No manuscript scenes are available to export.")

    lines = [
        "---",
        f"title: {_frontmatter_value(title)}",
        f"author: {_frontmatter_value(author)}",
        "generated_by: \"Literary Studio\"",
        *_provenance_frontmatter(provenance),
        "---",
        "",
    ]
    current_part: tuple[Any, str] | None = None
    current_chapter: tuple[Any, str] | None = None
    for scene_index, scene in enumerate(ordered):
        part_key = (scene.get("part_number"), str(scene.get("part_title") or ""))
        if part_key != current_part:
            part_title = _display_part_title(scene)
            if part_title:
                lines.extend([f"# {part_title}", ""])
            current_part = part_key
            current_chapter = None

        chapter_key = (scene.get("chapter_number"), str(scene.get("chapter_title") or ""))
        if chapter_key != current_chapter:
            chapter_title = _display_chapter_title(scene.get("chapter_title"))
            chapter_number = scene.get("chapter_number")
            chapter_heading = f"Chapter {chapter_number} - {chapter_title}" if chapter_number not in (None, 0) else chapter_title
            lines.extend([f"## {chapter_heading}", ""])
            current_chapter = chapter_key

        scene_heading = _scene_heading_text(scene)
        if scene_heading:
            # This directive is the durable taxonomy boundary. The formatter
            # must not infer a scene from title vocabulary (ANDANTE, LARGO,
            # etc.), because those words can also be tempo markings.
            lines.extend([
                "<!-- literary-style: scene-title locked -->",
                f"### {scene_heading}",
                "",
            ])
        scene_md = html_to_markdown(str(scene.get("content_html") or "")).strip()
        if scene_md:
            lines.extend([scene_md, ""])
            next_scene = ordered[scene_index + 1] if scene_index + 1 < len(ordered) else None
            same_chapter = next_scene is not None and (
                next_scene.get("chapter_number") == scene.get("chapter_number")
                and str(next_scene.get("chapter_title") or "") == str(scene.get("chapter_title") or "")
            )
            # A titled next scene supplies its own structural separator. Keep
            # a divider only for an untitled scene boundary, where there is no
            # metadata available to print.
            if same_chapter and not _scene_heading_text(next_scene):
                lines.extend(["* * *", ""])

    return "\n".join(lines).rstrip("\n") + "\n"


def _write_project_chapters(scenes: list[Mapping[str, Any]], source_dir: Path) -> list[Path]:
    grouped: dict[tuple[Any, str], list[Mapping[str, Any]]] = {}
    order: list[tuple[Any, str]] = []

    # Filter out non-manuscript material (worldbook, editorial passes, roundtables)
    valid_scenes = _exportable_scenes(scenes)

    for scene in valid_scenes:
        key = (scene.get("chapter_number"), str(scene.get("chapter_title") or "Unstructured material"))
        if key not in grouped:
            grouped[key] = []
            order.append(key)
        grouped[key].append(scene)

    ordered_keys = sorted(
        order,
        key=lambda key: _chapter_sort_key(
            {"chapter_number": key[0], "chapter_title": key[1]}, order.index(key)
        ),
    )
    paths: list[Path] = []
    for index, key in enumerate(ordered_keys):
        chapter_number, raw_title = key
        chapter_title = _display_chapter_title(raw_title)
        is_epigraph = "epigraph" in chapter_title.casefold()
        is_prologue = "prologue" in chapter_title.casefold()
        is_opening_matter = is_epigraph or is_prologue or (chapter_number is None and index == 0)
        try:
            chapter_num = int(chapter_number)
        except (TypeError, ValueError):
            chapter_num = 0 if is_opening_matter else index + 1
        act_match = re.search(r"act[-_ ](\d+)", raw_title, flags=re.IGNORECASE)
        act_number = int(act_match.group(1)) if act_match else (0 if is_opening_matter else chapter_num)
        chapter_id = f"ch{chapter_num:02d}-{'epigraph' if is_epigraph else ('prologue' if is_prologue else _safe_filename(chapter_title))}"
        path = source_dir / f"{chapter_id}.md"
        lines = [
            "---",
            f"title: {_frontmatter_value(chapter_title)}",
            f"chapter_id: {_frontmatter_value(chapter_id)}",
            f"act: {_frontmatter_value(act_number)}",
            f"part_title: {_frontmatter_value(_display_part_title(grouped[key][0], act_number))}",
            f"scene_taxonomy: {_frontmatter_value('project metadata governs scene labels and boundaries')}",
            "---",
            "",
            f"# {chapter_title}",
            "",
        ]
        sorted_chapter_scenes = sorted(
            grouped[key],
            key=lambda s: (s.get("scene_number_in_chapter") or 0, int(s.get("scene_number") or 0)),
        )
        for scene_idx, scene in enumerate(sorted_chapter_scenes):
            scene_heading = _scene_heading_text(scene)
            # A governed scene heading is the separator for named scenes. A
            # divider remains only where the source has no scene title to
            # print, so the reader never shows a bare break in place of
            # available metadata.
            if scene_idx > 0 and not scene_heading:
                lines.extend(["", "* * *", ""])
            # The global scene number is the stable key used by the Studio
            # database and compiler selection. Per-chapter scene ordinals are
            # intentionally not used here because every chapter may contain
            # a scene 1, scene 2, and so on.
            scene_number = scene.get("scene_number") or scene.get("scene_number_in_chapter")
            lines.append(
                f"<!-- literary-studio-scene: {scene_number} | {str(scene.get('title') or '').strip().replace('--', '—')} -->"
            )
            if scene_heading:
                lines.extend([
                    "<!-- literary-style: scene-title locked -->",
                    f"### {scene_heading}",
                    "",
                ])
            scene_md = html_to_markdown(str(scene.get("content_html") or "")).strip()
            if scene_md:
                lines.append(scene_md)

        path.write_text("\n".join(lines).strip() + "\n", encoding="utf-8")
        paths.append(path)
    return paths


def _latex_escape(value: Any) -> str:
    """Escape provenance values for the generated LaTeX footer."""
    text = str(value or "")
    replacements = {
        "\\": r"\textbackslash{}",
        "&": r"\&",
        "%": r"\%",
        "$": r"\$",
        "#": r"\#",
        "_": r"\_",
        "{": r"\{",
        "}": r"\}",
        "~": r"\textasciitilde{}",
        "^": r"\textasciicircum{}",
    }
    return "".join(replacements.get(char, char) for char in text)


def _footer_timestamp(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return "unknown time"
    text = text.replace("T", " ")
    if "." in text:
        text = text.split(".", 1)[0] + (" UTC" if "+00:00" in text else "")
    return text.replace("+00:00", " UTC")


def _provenance_footer(provenance: Mapping[str, Any]) -> str:
    author = _latex_escape(provenance.get("author") or DEFAULT_AUTHOR)
    generated_at = _latex_escape(_footer_timestamp(provenance.get("generated_at")))
    branch = _latex_escape(provenance.get("branch") or "detached")
    commit = str(provenance.get("commit_sha") or "unknown")[:12]
    return (
        rf"{author}\textbar\ {generated_at}\textbar\ Git\ {branch}\ @\ "
        rf"{_latex_escape(commit)}\textbar\ Page\ \thepage"
    )


def _compact_reader_layout(tex_path: Path) -> None:
    """Remove print-only blank-page padding from a Studio reader.

    Fiction Git Studio's book profiles intentionally insert empty recto/verso
    pages for a print edition. Literary Studio readers are review artifacts:
    they should move from a part or title page directly into the manuscript.
    Keep the authoritative typesetter unchanged and normalize only this
    generated, non-canonical LaTeX copy.
    """
    text = tex_path.read_text(encoding="utf-8")

    # The part divider pads to a recto page with one or two explicit null
    # pages. A reader does not need that print-only imposition.
    text = re.sub(
        r"\\clearpage\n\\makeatletter\n\\ifodd\\c@page\n"
        r"(?:  \\thispagestyle\{empty\}\\null\\clearpage\n)+"
        r"\\else\n"
        r"  \\thispagestyle\{empty\}\\null\\clearpage\n"
        r"\\fi\n\\makeatother\n",
        r"\\clearpage\n",
        text,
    )

    # Upstream title/part openers use this exact construct for a forced blank
    # page. Preserve the page break, but discard the empty page itself.
    text = text.replace(
        r"\thispagestyle{empty}\null\clearpage",
        r"\clearpage",
    )

    # Do not let repeated page breaks become visible spacing after the
    # normalization above.
    text = re.sub(r"(?:\\clearpage\n){2,}", r"\\clearpage\n", text)
    tex_path.write_text(text, encoding="utf-8")


def _inject_latex_provenance(
    tex_path: Path,
    provenance: Mapping[str, Any],
    page_start: int = 1,
    add_scene_labels: bool = False,
) -> None:
    """Add a small all-page provenance footer to formatter-generated LaTeX.

    The sibling formatter deliberately uses ``empty`` and ``plain`` page
    styles for title and chapter pages. Defining all three styles here keeps
    provenance on every physical page without changing the authoritative
    formatter or its project profiles.
    """
    text = tex_path.read_text(encoding="utf-8")
    footer = _provenance_footer(provenance)
    footer_block = "\n".join(
        [
            "% Literary Studio export provenance",
            # Five-point text was technically present but unreadable in the
            # generated readers. Seven-point text remains discreet while
            # making the author, timestamp, Git provenance, and page number
            # usable at normal print and screen zoom.
            r"\setlength{\footskip}{22pt}",
            rf"\newcommand{{\literarystudiofooter}}{{\fontsize{{7}}{{8}}\selectfont {footer}}}",
            r"\fancypagestyle{literary-studio}{%",
            r"  \fancyhf{}%",
            r"  \fancyfoot[C]{\literarystudiofooter}%",
            r"  \renewcommand{\headrulewidth}{0pt}%",
            r"  \renewcommand{\footrulewidth}{0pt}%",
            r"}",
            r"\fancypagestyle{plain}{%",
            r"  \fancyhf{}%",
            r"  \fancyfoot[C]{\literarystudiofooter}%",
            r"  \renewcommand{\headrulewidth}{0pt}%",
            r"  \renewcommand{\footrulewidth}{0pt}%",
            r"}",
            r"\fancypagestyle{empty}{%",
            r"  \fancyhf{}%",
            r"  \fancyfoot[C]{\literarystudiofooter}%",
            r"  \renewcommand{\headrulewidth}{0pt}%",
            r"  \renewcommand{\footrulewidth}{0pt}%",
            r"}",
        ]
    )
    if "% Literary Studio export provenance" not in text:
        text = text.replace(r"\begin{document}", footer_block + "\n\n\\begin{document}", 1)

    # The book class resets the page style and page counter at mainmatter.
    # Reapply the contract after that reset; frontmatter still receives the
    # footer through the document-level style.
    text = text.replace(
        r"\begin{document}",
        "\\begin{document}\n\\pagestyle{literary-studio}",
        1,
    )
    mainmatter = r"\mainmatter"
    if mainmatter in text:
        text = text.replace(
            mainmatter,
            f"{mainmatter}\n\\pagestyle{{literary-studio}}\n\\setcounter{{page}}{{{max(1, int(page_start))}}}",
            1,
        )
    else:
        text = text.replace(
            r"\pagestyle{literary-studio}",
            f"\\pagestyle{{literary-studio}}\n\\setcounter{{page}}{{{max(1, int(page_start))}}}",
            1,
        )

    # Scene comments are an internal bridge protocol, not reader content. The
    # external Markdown parser preserves them as literal LaTeX text, which is
    # why they previously appeared beneath chapter headings.
    marker = re.compile(
        r"<!--\s*literary-studio-scene:\s*(\d+)[^\n]*?-->(?:\\par)?"
    )
    if add_scene_labels:
        text = marker.sub(r"\\label{literary-studio-scene-\1}", text)
    else:
        text = marker.sub("", text)
    tex_path.write_text(text, encoding="utf-8")


def _scene_page_map(aux_path: Path) -> dict[int, int]:
    if not aux_path.exists():
        return {}
    mapping: dict[int, int] = {}
    pattern = re.compile(r"\\newlabel\{literary-studio-scene-(\d+)\}\{\{[^}]*\}\{(\d+)\}")
    for match in pattern.finditer(aux_path.read_text(encoding="utf-8", errors="replace")):
        mapping[int(match.group(1))] = int(match.group(2))
    return mapping


def build_full_book_scene_page_map(
    scenes: Iterable[Mapping[str, Any]],
    title: str,
    author: str,
    profile_id: str,
    work_dir: str | Path,
    provenance: Mapping[str, Any],
) -> dict[int, int]:
    """Compile a labeled full-book pass and return logical scene start pages."""
    export_latex, compile_latex_pdf, _ = _load_typesetter()
    root = Path(work_dir).resolve()
    source_dir = root / "sources"
    source_dir.mkdir(parents=True, exist_ok=True)
    chapter_files = _write_project_chapters(list(scenes), source_dir)
    if not chapter_files:
        return {}
    tex_path = root / f"{_safe_filename(title)}-page-map.tex"
    manifest = export_latex(
        [str(path) for path in chapter_files],
        tex_path,
        profile_id=profile_id,
        title=title,
        author=author,
    )
    _compact_reader_layout(tex_path)
    _inject_latex_provenance(tex_path, provenance, page_start=1, add_scene_labels=True)
    compile_result = compile_latex_pdf(manifest["tex_path"], runs=2)
    if not compile_result.get("compiled"):
        detail = compile_result.get("stderr") or "LaTeX page-map compilation failed."
        raise RuntimeError(detail[-4000:])
    return _scene_page_map(tex_path.with_suffix(".aux"))


def compile_shunn_pdf(
    scenes: Iterable[Mapping[str, Any]],
    output_path: str | Path,
    title: str,
    author: str,
    profile_id: str = DEFAULT_PROFILE_ID,
    provenance: Mapping[str, Any] | None = None,
    page_start: int = 1,
) -> Path:
    """Compile project scenes with the Fiction Git Studio LaTeX formatter."""
    export_latex, compile_latex_pdf, _ = _load_typesetter()
    destination = Path(output_path).resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    work_dir = destination.parent / ".typeset" / _safe_filename(profile_id)
    source_dir = work_dir / "sources"
    source_dir.mkdir(parents=True, exist_ok=True)
    chapter_files = _write_project_chapters(list(scenes), source_dir)
    if not chapter_files:
        raise ValueError("No manuscript scenes are available to format.")

    tex_path = work_dir / f"{_safe_filename(title)}.tex"
    manifest = export_latex(
        [str(path) for path in chapter_files],
        tex_path,
        profile_id=profile_id,
        title=title,
        author=author,
    )
    _compact_reader_layout(tex_path)
    if provenance:
        _inject_latex_provenance(tex_path, provenance, page_start=page_start)
    compile_result = compile_latex_pdf(manifest["tex_path"], runs=2)
    if not compile_result.get("compiled"):
        detail = compile_result.get("stderr") or "LaTeX compilation failed."
        raise RuntimeError(detail[-4000:])

    compiled_pdf = Path(compile_result["pdf_path"])
    shutil.copyfile(compiled_pdf, destination)
    return destination
