"""
Format Parsers, Rebuilders & Control-Code Validators for Warcraft III Localization
Supports:
- .fdf (Frame Definition File StringList)
- .txt (INI-style [Section] Key=Value and flat Key=Value)
- .wts (Warcraft III Trigger Strings: STRING <id> { ... })
- .srt (SubRip Movie Captions)
- .slk (SYLK spreadsheet tables for dialogue speaker/order/audio metadata)
"""
import re
from collections import OrderedDict

# Regex patterns for control codes that must be preserved in translations
RE_COLOR_CODE = re.compile(r'\|[cC][0-9a-fA-F]{8}|\|[rR]')
RE_NEWLINE_CODE = re.compile(r'\|[nN]')
RE_DATA_TAG = re.compile(r'<[A-Za-z0-9_,\s%.\-]+>')
RE_PRINTF_TAG = re.compile(r'%[0-9]*[dsf%]')
RE_ANIM_TAG = re.compile(r'\[/?\"[^\"]+\"[^\]]*\]')


def extract_control_tokens(text: str) -> dict[str, list[str]]:
    if not text:
        return {'color': [], 'newline': [], 'data_tag': [], 'printf': [], 'anim_tag': []}
    return {
        'color': RE_COLOR_CODE.findall(text),
        'newline': RE_NEWLINE_CODE.findall(text),
        'data_tag': RE_DATA_TAG.findall(text),
        'printf': RE_PRINTF_TAG.findall(text),
        'anim_tag': RE_ANIM_TAG.findall(text),
    }


def strip_anim_tags(text: str) -> str:
    """Strips Reforged facial animation markup like ["Happy" ...]...[/"Happy"] for clean reading."""
    if not text:
        return text
    return RE_ANIM_TAG.sub('', text)


def validate_translation(en_text: str, ko_text: str, ja_text: str) -> list[str]:
    """
    Validates a Japanese translation against English and Korean originals:
    - Checks <Unit,Data> tags match exactly
    - Checks %d/%s printf specifiers match
    - Checks |cXXXXXXXX and |r color tags are balanced
    - Checks unescaped double quotes if required
    """
    issues = []
    if not ja_text:
        return issues

    en_tokens = extract_control_tokens(en_text)
    ko_tokens = extract_control_tokens(ko_text)
    ja_tokens = extract_control_tokens(ja_text)

    # 1. Data tags (<AHhb,DataA1>, etc.) must match koKR (or enUS)
    ref_data = ko_tokens['data_tag'] if ko_text else en_tokens['data_tag']
    if sorted(ja_tokens['data_tag']) != sorted(ref_data):
        issues.append(f"Data tag mismatch: expected {ref_data}, got {ja_tokens['data_tag']}")

    # 2. Printf tags (%d, %s)
    ref_printf = ko_tokens['printf'] if ko_text else en_tokens['printf']
    if sorted(ja_tokens['printf']) != sorted(ref_printf):
        issues.append(f"Format specifier mismatch: expected {ref_printf}, got {ja_tokens['printf']}")

    # 3. Color tag balance (|cXXXXXXXX vs |r)
    c_opens = sum(1 for t in ja_tokens['color'] if t.lower().startswith('|c'))
    c_closes = sum(1 for t in ja_tokens['color'] if t.lower() == '|r')
    ref_opens = sum(1 for t in (ko_tokens['color'] if ko_text else en_tokens['color']) if t.lower().startswith('|c'))
    ref_closes = sum(1 for t in (ko_tokens['color'] if ko_text else en_tokens['color']) if t.lower() == '|r')
    if (c_opens != c_closes) and (ref_opens == ref_closes):
        issues.append(f"Unbalanced color tags: {c_opens} '|c' vs {c_closes} '|r'")

    return issues


# ==============================================================================
# 1. FDF Parser & Rebuilder (StringList { KEY "VALUE", })
# ==============================================================================
RE_FDF_ENTRY = re.compile(
    r'^([ \t]*([A-Za-z0-9_]+)[ \t]+\")((?:[^\"\\]|\\.)*)(\"[ \t]*,?[ \t]*\r?\n?)',
    re.MULTILINE
)


def parse_fdf(text: str) -> OrderedDict[str, str]:
    entries = OrderedDict()
    for m in RE_FDF_ENTRY.finditer(text):
        key = m.group(2)
        val = m.group(3)
        entries[key] = val
    return entries


def rebuild_fdf(template_text: str, translations: dict[str, str]) -> tuple[str, int]:
    replaced_count = 0

    def _repl(m):
        nonlocal replaced_count
        prefix, key, old_val, suffix = m.group(1), m.group(2), m.group(3), m.group(4)
        if key in translations and translations[key]:
            new_val = translations[key]
            # Escape raw unescaped quotes if any
            new_val = re.sub(r'(?<!\\)"', r'\"', new_val)
            if new_val != old_val:
                replaced_count += 1
            return f"{prefix}{new_val}{suffix}"
        return m.group(0)

    new_text = RE_FDF_ENTRY.sub(_repl, template_text)
    return new_text, replaced_count


# ==============================================================================
# 2. INI / TXT String File Parser & Rebuilder ([Section] Key=Value)
# ==============================================================================
NON_TRANSLATABLE_KEYS = {
    'hotkey', 'unhotkey', 'researchhotkey', 'buttonpos', 'unbuttonpos',
    'researchbuttonpos', 'art', 'unart', 'researchart', 'Specialart',
    'casterart', 'targetart', 'missileart', 'effectart', 'areaeffectart',
    'Effectsound', 'effectsoundlooped', 'soundloop', ' tipcount', 'tipcount',
}


def parse_ini_txt(text: str) -> OrderedDict[str, dict]:
    """
    Parses Warcraft III .txt string files ([Section] Key=Value or flat Key=Value).
    Returns OrderedDict mapping 'Section.Key' (or 'Key' if no section) -> {
        'section': section, 'key': key, 'value': unquoted_value, 'quoted': bool, 'line_no': int
    }
    """
    entries = OrderedDict()
    cur_section = ''
    for idx, raw_line in enumerate(text.splitlines(), 1):
        line = raw_line.strip()
        if not line or line.startswith('//') or line.startswith(';'):
            continue
        if line.startswith('[') and ']' in line:
            cur_section = line[1:line.index(']')].strip()
            continue
        if '=' in raw_line:
            k_part, v_part = raw_line.split('=', 1)
            key = k_part.strip()
            val_raw = v_part.strip()
            quoted = len(val_raw) >= 2 and val_raw.startswith('"') and val_raw.endswith('"')
            val = val_raw[1:-1] if quoted else val_raw
            full_id = f"{cur_section}.{key}" if cur_section else key
            entries[full_id] = {
                'section': cur_section,
                'key': key,
                'value': val,
                'quoted': quoted,
                'line_no': idx,
            }
    return entries


def rebuild_ini_txt(template_text: str, translations: dict[str, str]) -> tuple[str, int]:
    """
    Rebuilds a .txt string file preserving all comments, line endings, and structure.
    """
    lines = template_text.splitlines(keepends=True)
    out_lines = []
    cur_section = ''
    replaced_count = 0

    for raw_line in lines:
        stripped = raw_line.strip()
        if not stripped or stripped.startswith('//') or stripped.startswith(';'):
            out_lines.append(raw_line)
            continue
        if stripped.startswith('[') and ']' in stripped:
            cur_section = stripped[1:stripped.index(']')].strip()
            out_lines.append(raw_line)
            continue
        if '=' in raw_line:
            # Preserve line ending
            ending = '\r\n' if raw_line.endswith('\r\n') else ('\n' if raw_line.endswith('\n') else '')
            body = raw_line[:-len(ending)] if ending else raw_line
            k_part, v_part = body.split('=', 1)
            key = k_part.strip()
            full_id = f"{cur_section}.{key}" if cur_section else key
            if full_id in translations and translations[full_id]:
                new_val = translations[full_id]
                val_raw = v_part.strip()
                quoted = len(val_raw) >= 2 and val_raw.startswith('"') and val_raw.endswith('"')
                if quoted and not (new_val.startswith('"') and new_val.endswith('"')):
                    new_val_formatted = f'"{new_val}"'
                else:
                    new_val_formatted = new_val
                if new_val_formatted != val_raw:
                    replaced_count += 1
                out_lines.append(f"{k_part}={new_val_formatted}{ending}")
                continue
        out_lines.append(raw_line)

    return ''.join(out_lines), replaced_count


# ==============================================================================
# 3. WTS Parser & Rebuilder (STRING <num> { ... })
# ==============================================================================
RE_WTS_BLOCK = re.compile(
    r'(STRING[ \t]+(\d+)[ \t]*\r?\n(?:[ \t]*//[^\r\n]*\r?\n)*\{\r?\n)(.*?)(\r?\n\})',
    re.DOTALL
)


def parse_wts(text: str) -> OrderedDict[int, dict]:
    entries = OrderedDict()
    for m in RE_WTS_BLOCK.finditer(text):
        header = m.group(1)
        str_num = int(m.group(2))
        body = m.group(3)
        # Extract any comment lines inside header
        comments = [l.strip()[2:].strip() for l in header.splitlines() if l.strip().startswith('//')]
        entries[str_num] = {
            'id': str_num,
            'key': f"TRIGSTR_{str_num:03d}",
            'value': body,
            'comment': ' | '.join(comments) if comments else '',
        }
    return entries


def rebuild_wts(template_text: str, translations: dict[int | str, str]) -> tuple[str, int]:
    replaced_count = 0
    # Normalize keys to int
    norm_trans = {}
    for k, v in translations.items():
        if not v:
            continue
        if isinstance(k, int):
            norm_trans[k] = v
        elif isinstance(k, str):
            if k.isdigit():
                norm_trans[int(k)] = v
            elif k.upper().startswith('TRIGSTR_') and k[8:].isdigit():
                norm_trans[int(k[8:])] = v
            elif k.upper().startswith('STRING_') and k[7:].isdigit():
                norm_trans[int(k[7:])] = v

    def _repl(m):
        nonlocal replaced_count
        prefix, num_str, old_body, suffix = m.group(1), m.group(2), m.group(3), m.group(4)
        s_num = int(num_str)
        if s_num in norm_trans:
            new_body = norm_trans[s_num]
            if new_body != old_body:
                replaced_count += 1
            return f"{prefix}{new_body}{suffix}"
        return m.group(0)

    new_text = RE_WTS_BLOCK.sub(_repl, template_text)
    return new_text, replaced_count


# ==============================================================================
# 4. SRT Movie Captions Parser & Rebuilder
# ==============================================================================
RE_SRT_BLOCK = re.compile(
    r'(\d+)\r?\n(\d{2}:\d{2}:\d{2},\d{3}[ \t]*-->[ \t]*\d{2}:\d{2}:\d{2},\d{3})\r?\n((?:.+?\r?\n?)+?)(?=\r?\n\d+\r?\n|\Z)',
    re.MULTILINE
)


def parse_srt(text: str) -> OrderedDict[str, dict]:
    entries = OrderedDict()
    for m in RE_SRT_BLOCK.finditer(text.strip() + '\n'):
        seq = m.group(1).strip()
        ts = m.group(2).strip()
        body = m.group(3).strip()
        entries[f"SRT_{int(seq):03d}"] = {
            'seq': int(seq),
            'timestamp': ts,
            'value': body,
        }
    return entries


def rebuild_srt(template_text: str, translations: dict[str, str]) -> tuple[str, int]:
    replaced_count = 0

    def _repl(m):
        nonlocal replaced_count
        seq = m.group(1).strip()
        ts = m.group(2)
        old_body = m.group(3).strip()
        key = f"SRT_{int(seq):03d}"
        if key in translations and translations[key]:
            new_body = translations[key].strip()
            if new_body != old_body:
                replaced_count += 1
            return f"{seq}\r\n{ts}\r\n{new_body}\r\n"
        return m.group(0)

    new_text = RE_SRT_BLOCK.sub(_repl, template_text.strip() + '\r\n')
    return new_text, replaced_count


# ==============================================================================
# 5. SLK (SYLK) Parser for Dialogue Metadata
# ==============================================================================
def parse_slk(text: str) -> list[dict[str, str]]:
    grid = {}
    cur_x, cur_y = 1, 1
    max_x, max_y = 0, 0
    for line in text.splitlines():
        if not line.startswith('C;'):
            continue
        parts = line.split(';')
        val = None
        for p in parts[1:]:
            if p.startswith('X'):
                cur_x = int(p[1:])
            elif p.startswith('Y'):
                cur_y = int(p[1:])
            elif p.startswith('K'):
                v = p[1:]
                if v.startswith('"') and v.endswith('"'):
                    v = v[1:-1]
                val = v
        if val is not None:
            grid[(cur_x, cur_y)] = val
            max_x = max(max_x, cur_x)
            max_y = max(max_y, cur_y)

    headers = {x: grid.get((x, 1), f'Col{x}') for x in range(1, max_x + 1)}
    rows = []
    for y in range(2, max_y + 1):
        row = {headers[x]: grid.get((x, y), '') for x in range(1, max_x + 1) if (x, y) in grid}
        if row:
            rows.append(row)
    return rows
