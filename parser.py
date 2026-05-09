"""
CCORAL v2 — Structural System Prompt Parser
=============================================

Parses Claude Code's system prompt into a tree of sections,
enabling surgical replacement without fragile regex matching.

The API sends system prompts as a list of content blocks:
  [{"type": "text", "text": "...", "cache_control": {...}}, ...]

Within text blocks, sections are delimited by:
  - Markdown headers (# Section Name)
  - XML tags (<system-reminder>, <available-deferred-tools>, etc.)
  - The identity sentence ("You are Claude Code...")

The parser builds a tree: Blocks → Sections → Content
Decisions happen at the section level: keep / strip / replace.
"""

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

log = logging.getLogger("ccoral.parser")


@dataclass
class Section:
    """A logical section within a system prompt block."""
    name: str           # Identifier (e.g., "doing_tasks", "environment", "identity")
    content: str        # Raw text content
    section_type: str   # "markdown_header", "xml_tag", "identity", "unknown"
    header_level: int = 0  # For markdown headers: 1 = #, 2 = ##, etc.
    keep: bool = True   # Whether to preserve this section


@dataclass
class Block:
    """A content block from the API payload."""
    index: int
    text: str
    cache_control: Optional[dict] = None
    sections: list[Section] = field(default_factory=list)
    original: Optional[dict] = None  # Original API block dict


# Known section identifiers and their canonical names.
# Maps (header_text or XML tag, lowercased) → canonical_name.
#
# Refreshed for Claude Code 2.1.138 (May 2026). The CC system prompt is
# assembled from ~25-50 fragments and headers come and go between minor
# versions. The current set:
#   - Headers PRESENT in modern CC: # Harness, # Executing actions with care,
#     # Text output (does not apply to tool calls), # Committing changes with git,
#     # Creating pull requests, # Other common operations, # Environment.
#   - A few legacy headers from 2.1.13x and earlier are still kept (# System,
#     # Doing tasks, # Tone and style, # auto memory) so the parser tolerates
#     a fixture range. Dead matchers from older versions ("using your tools",
#     "tool usage policy", "output efficiency", "important: you must never",
#     "fast_mode_info") have been dropped — see PROFILE_SCHEMA.md for the
#     deprecated list.
#   - HEADERLESS prose fragments (action-safety, doing-tasks, tool-usage,
#     tone-and-style, agent-thread-notes, memory-instructions) are matched
#     via PROSE_FRAGMENT_LEAD_SENTENCES below.
SECTION_IDENTIFIERS = {
    # Identity
    "you are claude code": "identity",

    # Core behavior — modern (2.1.124+)
    "harness": "harness",
    "text output (does not apply to tool calls)": "text_output",
    "text output": "text_output",  # fallback if header gets trimmed/folded
    "executing actions with care": "executing_actions",

    # Core behavior — legacy headers still observed in 2.1.13x fixtures
    "system": "system",
    "doing tasks": "doing_tasks",
    "tone and style": "tone_style",
    "auto memory": "auto_memory",

    # Environment & config
    "environment": "environment",
    "committing changes with git": "git_commit",
    "creating pull requests": "pull_requests",
    "other common operations": "other_operations",

    # Security
    "important: assist with authorized": "security_policy",

    # XML sections
    # NB: case-folded via xml_match.group(1).lower() before equality check.
    "available-deferred-tools": "deferred_tools",  # dead in 2.1.x but cheap; keep for legacy
    "system-reminder": "system_reminder",
    "command-message": "command_message",
    "command-name": "command_name",
    "command-args": "command_args",

    # Claude.md / project instructions
    "claudemd": "claude_md",
    "currentdate": "current_date",
    "memory index": "memory_index",
}


# Prose fragments that arrive WITHOUT a markdown header — match by leading
# substring. List of (lead_substring, canonical_name) tuples. Matching is
# case-insensitive, longest-prefix-wins. Lead substrings copied verbatim
# from system-prompts/system-prompt-*.md in
# https://github.com/Piebald-AI/claude-code-system-prompts (v2.1.124+).
#
# Section type for these is "prose_fragment" — does NOT participate in
# XML close-tag tracking.
PROSE_FRAGMENT_LEAD_SENTENCES: list[tuple[str, str]] = [
    # action-safety-and-truthful-reporting (2.1.136+)
    (
        "For actions that are hard to reverse or outward-facing, confirm first unless durably authorized",
        "action_safety",
    ),
    # doing-tasks-software-engineering-focus
    (
        "The user will primarily request you to perform software engineering tasks.",
        "doing_tasks",
    ),
    # tool-usage-task-management
    (
        "Break down and manage your work with the",
        "tool_usage",
    ),
    # tool-usage-subagent-guidance
    (
        "Use the ${TASK_TOOL_NAME} tool with specialized agents when the task at hand matches",
        "tool_usage",
    ),
    # parallel-tool-call-note
    (
        "You can call multiple tools in a single response. If you intend to call multiple tools",
        "tool_usage",
    ),
    # tone-and-style-code-references
    (
        "When referencing specific functions or pieces of code include the pattern file_path:line_number",
        "tone_style",
    ),
    # tone-and-style-concise-output-short
    (
        "Your responses should be short and concise.",
        "tone_style",
    ),
    # agent-thread-notes (subagent-only)
    (
        "- Agent threads always have their cwd reset between bash calls, as a result please only use absolute file paths.",
        "agent_thread_notes",
    ),
    # memory-instructions
    (
        "You have a persistent file-based memory",
        "memory_instructions",
    ),
]


def _identify_section(line: str) -> tuple[Optional[str], str, int]:
    """
    Identify what section a line starts.

    Returns: (canonical_name, section_type, header_level) or (None, "", 0)
    """
    stripped = line.strip()

    # XML opening tags
    xml_match = re.match(r'^<([a-zA-Z_-]+)(?:\s|>)', stripped)
    if xml_match:
        tag = xml_match.group(1).lower()
        for pattern, name in SECTION_IDENTIFIERS.items():
            if tag == pattern:
                return name, "xml_tag", 0

    # Markdown headers
    header_match = re.match(r'^(#{1,4})\s+(.+)', stripped)
    if header_match:
        level = len(header_match.group(1))
        header_text = header_match.group(2).strip().lower()
        for pattern, name in SECTION_IDENTIFIERS.items():
            if header_text.startswith(pattern):
                return name, "markdown_header", level
        # Unknown header — still a section boundary
        slug = re.sub(r'[^a-z0-9]+', '_', header_text).strip('_')
        return f"header_{slug}", "markdown_header", level

    # Billing/metadata headers (must always be preserved)
    if stripped.startswith("x-anthropic-"):
        return "_billing", "metadata", 0

    # Identity sentence
    if stripped.lower().startswith("you are claude code"):
        return "identity", "identity", 0

    # Prose fragments without headers (longest-prefix-wins).
    # Modern CC ships several behavioral fragments as bare paragraphs;
    # we match by leading substring against PROSE_FRAGMENT_LEAD_SENTENCES.
    lower = stripped.lower()
    best_lead = ""
    best_name: Optional[str] = None
    for lead, name in PROSE_FRAGMENT_LEAD_SENTENCES:
        if lower.startswith(lead.lower()) and len(lead) > len(best_lead):
            best_lead = lead
            best_name = name
    if best_name is not None:
        return best_name, "prose_fragment", 0

    # IMPORTANT: lines (standalone policy statements)
    if stripped.startswith("IMPORTANT:"):
        lower = stripped.lower()
        for pattern, name in SECTION_IDENTIFIERS.items():
            if lower.startswith(pattern.lower()):
                return name, "important", 0

    return None, "", 0


def parse_text_block(text: str) -> list[Section]:
    """Parse a text block into sections."""
    lines = text.split('\n')
    sections = []
    current_name = None
    current_type = "unknown"
    current_level = 0
    current_lines = []
    in_xml = False
    xml_tag = None

    for line in lines:
        # Check for XML closing tag
        if in_xml and xml_tag:
            current_lines.append(line)
            if re.match(rf'^</{xml_tag}', line.strip()):
                in_xml = False
            continue

        # Check for new section
        name, stype, level = _identify_section(line)

        if name is not None:
            # Save previous section
            if current_name is not None:
                sections.append(Section(
                    name=current_name,
                    content='\n'.join(current_lines),
                    section_type=current_type,
                    header_level=current_level,
                ))
            elif current_lines:
                # Content before first identified section
                sections.append(Section(
                    name="_preamble",
                    content='\n'.join(current_lines),
                    section_type="preamble",
                ))

            current_name = name
            current_type = stype
            current_level = level
            current_lines = [line]

            # Track XML blocks to capture until closing tag
            if stype == "xml_tag":
                xml_match = re.match(r'^<([a-zA-Z_-]+)', line.strip())
                if xml_match:
                    xml_tag = xml_match.group(1)
                    in_xml = True
        else:
            current_lines.append(line)

    # Save final section
    if current_name is not None:
        sections.append(Section(
            name=current_name,
            content='\n'.join(current_lines),
            section_type=current_type,
            header_level=current_level,
        ))
    elif current_lines:
        sections.append(Section(
            name="_preamble",
            content='\n'.join(current_lines),
            section_type="preamble",
        ))

    return sections


def parse_system_prompt(system: list | str) -> list[Block]:
    """
    Parse the full system prompt from the API payload.

    Handles both string and list-of-blocks formats.
    """
    if isinstance(system, str):
        block = Block(index=0, text=system)
        block.sections = parse_text_block(system)
        return [block]

    blocks = []
    for i, item in enumerate(system):
        if isinstance(item, dict):
            text = item.get("text", "")
            cache = item.get("cache_control")
            original = item
        elif isinstance(item, str):
            text = item
            cache = None
            original = {"type": "text", "text": text}
        else:
            continue

        block = Block(index=i, text=text, cache_control=cache, original=original)
        block.sections = parse_text_block(text)
        blocks.append(block)

    return blocks


def rebuild_system_prompt(blocks: list[Block]) -> list[dict]:
    """Rebuild the API system prompt from (potentially modified) blocks."""
    result = []
    for block in blocks:
        # Rebuild text from kept sections
        kept = [s.content for s in block.sections if s.keep]
        text = '\n\n'.join(kept) if kept else ""

        if not text.strip():
            continue

        entry = {"type": "text", "text": text}
        if block.cache_control:
            entry["cache_control"] = block.cache_control
        result.append(entry)

    return result


def apply_profile(blocks: list[Block], profile: dict) -> list[Block]:
    """
    Apply a profile's keep/strip/inject rules to parsed blocks.

    Profile dict:
        inject: str          — content to inject (replaces identity)
        preserve: list[str]  — section names to keep
        strip: "all_else"    — strip everything not in preserve
        minimal: bool        — if true, strip everything, just inject
    """
    inject_text = profile.get("inject", "").strip()
    preserve = set(profile.get("preserve", []))
    minimal = profile.get("minimal", False)
    strict = profile.get("strict", False)

    # "all" means keep everything
    keep_all = "all" in preserve

    if keep_all:
        # Passthrough — don't touch anything
        return blocks

    # Map friendly names to canonical section names
    preserve_map = {
        "environment": "environment",
        "hooks": "hooks_info",
        "mcp": "deferred_tools",
        "tools": "deferred_tools",
        "claude_md": "claude_md",
        "memory": "memory_index",
        "system": "system",
        "current_date": "current_date",
        # CC 2.1.124+ canonicals — let profiles list these by friendly name.
        "harness": "harness",
        "text_output": "text_output",
        "action_safety": "action_safety",
        "tool_usage": "tool_usage",
        "tone_style": "tone_style",
        "agent_thread_notes": "agent_thread_notes",
        "memory_instructions": "memory_instructions",
    }
    canonical_preserve = set()
    for p in preserve:
        canonical_preserve.add(preserve_map.get(p, p))

    # Default preserves unless minimal or strict — keep it tight:
    # environment (OS/shell/cwd), tools (MCP definitions), current_date, claude_md,
    # and harness (since identity now lives inside `# Harness` and the inject path
    # only replaces the identity sentence — without preserving harness, the
    # bullets that contextualize the agent get dropped).
    # Notably NOT preserved by default: system_reminder, memory_index, _preamble
    if not minimal and not strict:
        canonical_preserve.update(
            {"environment", "deferred_tools", "current_date", "claude_md", "harness"}
        )

    # Custom .md file support — profile can specify e.g. claude_md_name: ALBERT.md
    custom_md_name = profile.get("claude_md_name")
    custom_md_content = None
    if custom_md_name:
        # Try to find working directory from environment section
        cwd = None
        for block in blocks:
            for section in block.sections:
                if section.name == "environment":
                    for env_line in section.content.split('\n'):
                        if 'working directory' in env_line.lower():
                            parts = env_line.split(':', 1)
                            if len(parts) == 2:
                                cwd = parts[1].strip()
                                break
                    break
            if cwd:
                break

        if cwd:
            custom_path = Path(cwd) / custom_md_name
            if custom_path.exists():
                custom_md_content = custom_path.read_text()

    injected = False
    for block in blocks:
        for section in block.sections:
            if section.name == "identity" and inject_text:
                # Replace identity with injection
                section.content = inject_text
                section.keep = True
                injected = True
            elif section.name == "claude_md" and custom_md_content is not None:
                # Replace CLAUDE.md content with custom profile-specific file
                section.content = custom_md_content
                section.keep = True
            elif section.name == "_billing":
                # Billing/metadata — ALWAYS keep, required by API (even in minimal mode)
                section.keep = True
            elif minimal:
                section.keep = False
            elif section.name in canonical_preserve:
                section.keep = True
            elif section.name.startswith("_"):
                # Preambles — strip unless explicitly preserved
                section.keep = False
            else:
                section.keep = False

    # If we never found an identity section, prepend injection to first block
    if not injected and inject_text and blocks:
        inject_section = Section(
            name="injection",
            content=inject_text,
            section_type="injection",
        )
        blocks[0].sections.insert(0, inject_section)

    # Log what was kept vs stripped
    kept = []
    stripped = []
    for block in blocks:
        for s in block.sections:
            (kept if s.keep else stripped).append(s.name)
    if kept or stripped:
        log.info("[KEEP] %s", ", ".join(kept) if kept else "(none)")
        log.info("[STRIP] %s", ", ".join(stripped) if stripped else "(none)")

    return blocks


def dump_tree(blocks: list[Block]) -> str:
    """Debug: print the section tree."""
    lines = []
    for block in blocks:
        lines.append(f"Block {block.index} ({len(block.text)} chars)"
                      f"{' [cached]' if block.cache_control else ''}")
        for s in block.sections:
            status = "KEEP" if s.keep else "STRIP"
            preview = s.content[:80].replace('\n', '\\n')
            lines.append(f"  [{status}] {s.name} ({s.section_type}) — {preview}...")
    return '\n'.join(lines)
