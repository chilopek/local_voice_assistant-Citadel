"""Deterministic, extractive speech previews; the caller keeps the full chat answer.

Only the standard library is used. This is deliberately not a summarizer: when a
safe prefix cannot fit, referring to chat is preferable to guessing its meaning.
"""
from __future__ import annotations

import re


_LIMIT = 420
_NOTICE = "Подробности — в чате."
_CODE_NOTICE = "Код — в чате."
_BULLET = re.compile(r"^\s*(?:[-+*•]\s+|\d{1,4}[.)]\s+|\[[ xX]\]\s+)")
_CLAUSE_QUALIFIER = re.compile(
    r"\b(?:не|нет|нельзя|без|но|однако|только|если|кроме|при условии|"
    r"лишь|пока|частич\w*|ошиб\w*|сбо\w*|неуда\w*|недоступ\w*|огранич\w*|"
    r"отказ\w*|авар\w*|запрещ\w*|предупрежд\w*|"
    r"not|no|never|but|however|only|unless|except|if|"
    r"fail\w*|error\w*|unable|unavailable|warning\w*|denied|partial\w*|pending|"
    r"\w+n['’]t|cannot)\b", re.I,
)
_SUCCESS = re.compile(
    r"\b(?:готово|успешно|создан\w*|установлен\w*|открыт\w*|сохран[её]н\w*|"
    r"скопирован\w*|запущен\w*|перезапущен\w*|заверш[её]н\w*|выполнен\w*|"
    r"создал\w*|установил\w*|открыл\w*|сохранил\w*|скопировал\w*|запустил\w*|"
    r"выполнил\w*|удалил\w*|удал[её]н\w*|"
    r"done|created|installed|opened|saved|copied|started|restarted|completed|successfully|"
    r"deleted|removed|updated|uploaded|downloaded)\b", re.I,
)
_FAILURE = re.compile(
    r"\b(?:не\s+(?:удал[ао]?сь|получилось|смог\w*|(?:был[аои]?\s+)?"
    r"(?:выполнен\w*|заверш[её]н\w*|сохран[её]н\w*|создан\w*|установлен\w*|"
    r"открыт\w*|запущен\w*|прош[её]л\w*|прошла|прошли|открыл\w*|"
    r"установил\w*|сохранил\w*|сработал\w*|загрузил\w*|отвечает))|"
    r"ошибка\w*|сбой|сбоем|аварийно|отказано|недоступ[еён]\w*|"
    r"частично|отменен\w*|отменён\w*|(?:выполнен\w*|заверш[её]н\w*)\s+(?:лишь|только)|"
    r"(?:failed|failure|error|unable|denied|unavailable|partially|incomplete|cancelled|canceled)|"
    r"connection\s+refused|timed\s+out|"
    r"(?:didn['’]t|couldn['’]t|cannot|can['’]t|not)\s+"
    r"(?:complete\w*|finish\w*|save\w*|open\w*|install\w*|start\w*|upload\w*|final)|"
    r"completed\s+only)\b", re.I,
)
_HYPOTHETICAL = re.compile(
    r"\b(?:если|когда|при возникновении|например|в случае|if|when|unless|for example)\b", re.I,
)


def _is_failure(text: str) -> bool:
    """Recognize reported outcomes, not every negative condition in a how-to.

    Work sentence by sentence so a conditional instruction cannot mask a later
    actual failure. Negated errors and hypothetical examples are not outcomes.
    """
    for sentence in _sentences(text):
        positive = re.sub(
            r"\b(?:без\s+ошиб\w*|ошиб\w*\s+(?:нет|не\s+было)|"
            r"(?:no|without)\s+(?:errors?|failures?)|(?:did\s+not|didn['’]t)\s+fail)\b",
            "", sentence, flags=re.I,
        )
        failure = _FAILURE.search(positive)
        hypothetical = _HYPOTHETICAL.search(positive)
        if failure and (not hypothetical or hypothetical.start() > failure.start()):
            return True
    return False


_ABBREVIATIONS = {
    "т", "д", "п", "е", "др", "пр", "напр", "см", "рис", "стр", "им",
    "г", "гг", "ул", "руб", "коп", "тыс", "млн", "млрд", "ч", "мин", "сек",
    "mr", "mrs", "ms", "dr", "prof", "vs", "etc", "e", "g", "i", "a.m", "p.m",
}


def _inline(text: str) -> str:
    # Keep link labels, never image alt text or reference definitions.
    text = re.sub(r"!\[[^\]]*\]\([^\n]*?\)", "", text)
    text = re.sub(r"\[\d+[\d, ;–-]*\]\([^\n]*?\)", "", text)
    text = re.sub(r"\[([^\]]+)\]\((?:[^()\s]|\([^()]*\))*\)", r"\1", text)
    text = re.sub(r"\[([^\]]+)\]\[[^\]]*\]", r"\1", text)
    text = re.sub(r"【[^】]*】|[^]*|\[(?:\d+[\d, ;–-]*|\^[^\]]+)\]", "", text)
    # Preserve sentence punctuation after a removed URL, including autolinks.
    text = re.sub(
        r"(?:https?://|www\.)[^\s<>]+",
        lambda m: m[0][len(m[0].rstrip('.,!?;:')):], text,
    )
    text = re.sub(r"\b(?:[a-z0-9-]+\.)+(?:[a-z]{2,63}|рф)\b(?:/[^\s<>]*)?",
                  "", text, flags=re.I)
    text = re.sub(r"<[^>]*>|[<>]", "", text)
    text = re.sub(r"\(\s*\)|\[\s*\]", "", text)
    text = re.sub(r"`([^`]+)`", r"\1", text)
    text = re.sub(r"(?<!\w)[*_~]+|[*_~]+(?!\w)", "", text)
    text = re.sub(r"\s+([,.;:!?])", r"\1", text)
    return re.sub(r"\s+", " ", text).strip()


def _blocks(answer: str) -> tuple[list[tuple[str, bool]], bool, int, list[str]]:
    blocks: list[tuple[str, bool]] = []
    pending: list[str] = []
    is_list = False
    fence = ""
    removed = False
    code_size = 0
    hidden_outcomes: list[str] = []

    def remember_outcome(text: str) -> None:
        if "|" in text:
            text = ": ".join(cell.strip() for cell in text.split("|") if cell.strip())
        value = _inline(re.sub(r"^#{1,6}\s+", "", text))
        if _is_failure(value):
            hidden_outcomes.append(value)

    def flush() -> None:
        if pending:
            value = _inline(" ".join(pending))
            if value and re.search(r"\w", value):
                blocks.append((value, is_list))
            pending.clear()

    for line in answer.splitlines():
        stripped = line.strip()
        marker = re.match(r"^(`{3,}|~{3,})", stripped)
        if fence:
            removed = True
            code_size += len(line)
            if marker and marker[1][0] == fence[0] and len(marker[1]) >= len(fence):
                fence = ""
            continue
        if marker:
            flush()
            fence = marker[1]
            removed = True
            continue
        if line.startswith(("    ", "\t")) and not is_list:
            flush()
            removed = True
            code_size += len(line)
            continue
        if re.fullmatch(r"(?:={3,}|-{3,})", stripped) and pending and not is_list:
            if len(pending) == 1 and not re.search(r"[.!?…]$", pending[0]):
                remember_outcome(pending[0])
                pending.clear()  # Setext heading, not prose.
            else:
                flush()
            removed = True
            continue
        if (re.match(r"^#{1,6}\s", stripped)
                or re.fullmatch(r"(?:\*\*[^*]+\*\*|__[^_]+__):?", stripped)
                or re.fullmatch(r"[-=*_ ]{3,}", stripped)
                or re.match(r"^\[[^\]]+\]:", stripped)
                or stripped.count("|") >= 2):
            flush()
            removed = True
            remember_outcome(stripped)
            continue
        if not stripped:
            flush()
            is_list = False
            continue
        bullet = _BULLET.match(line)
        if bullet:
            flush()
            is_list = True
            stripped = line[bullet.end():]
            stripped = re.sub(r"^\[[ xX]\]\s*", "", stripped)
        elif is_list and not line[:1].isspace():
            flush()
            is_list = False
        pending.append(re.sub(r"^>\s?", "", stripped))
    flush()
    return blocks, removed, code_size, hidden_outcomes


def _sentences(text: str) -> list[str]:
    """Split terminal punctuation, protecting decimals, initials and abbreviations."""
    result: list[str] = []
    start = 0
    for match in re.finditer(r'[.!?…]+[»”"\')\]]*(?=\s|$)', text):
        end = match.end()
        if match[0] == "." and end < len(text):
            token = re.search(r"([\w.]+)\.$", text[:end])
            word = token[1].lower() if token else ""
            if word in _ABBREVIATIONS or (len(word) == 1 and word.isalpha()):
                continue
        result.append(text[start:end].strip())
        start = end
    tail = text[start:].strip()
    if tail:
        result.append(tail)
    return result


def _finish(text: str) -> str:
    return text if re.search(r'[.!?…][»”"\')\]]*$', text) else text.rstrip(";:, ") + "."


def concise_speech(answer: str) -> str:
    """Return at most 420 characters: first 1–3 sentences or up to 3 list items.

    Truncation carries a chat notice. Ordinary explanations use a prefix;
    action reports also include later failed outcomes, replacing success claims
    if needed. Bullets and qualifiers stay atomic. A long sentence may end at a
    semicolon only without a qualifying tail. No model, network or voice backend.
    """
    if not answer.strip():
        return ""
    blocks, removed, code_size, hidden_outcomes = _blocks(answer)
    if not blocks:
        return _CODE_NOTICE if code_size or "```" in answer or "~~~" in answer else _NOTICE
    prose = " ".join(text for text, _ in blocks)
    if code_size > 2 * len(prose) and not _is_failure(prose) and not hidden_outcomes:
        return _CODE_NOTICE

    units: list[str] = []
    for text, listed in blocks:
        # A bullet is an atomic item: do not speak its success and drop its caveat.
        units.extend([text] if listed else _sentences(text))
    budget = _LIMIT - len(_NOTICE) - 1
    selected: list[str] = []
    consumed = 0
    clause_tail = ""
    for unit in units:
        if len(selected) == 3:
            break
        # Removing a link can leave a dangling phrase ("available at."). It is
        # safer to defer to chat than to turn it into an invented completion.
        if re.search(r"\b(?:на|по|в|из|от|для|at|on|from|via|see)\s*[.!?…]*$", unit, re.I):
            break
        finished = _finish(unit)
        candidate = " ".join([*selected, finished])
        if len(candidate) > budget:
            # Full short answers can use the space reserved for a notice.
            if consumed == len(units) - 1 and not removed and len(candidate) <= _LIMIT:
                selected.append(finished)
                consumed += 1
                break
            if not selected and not blocks[0][1]:
                for separator in re.finditer(r";\s+", unit):
                    prefix = unit[:separator.start()].strip()
                    if (len(_finish(prefix)) <= budget and len(prefix.split()) >= 4
                            and not _CLAUSE_QUALIFIER.search(unit[separator.end():])):
                        selected = [_finish(prefix)]
                        clause_tail = unit[separator.end():]
                        break
            break
        selected.append(finished)
        consumed += 1

    # Only reported failures qualify for promotion, and only when the prefix
    # claims an action succeeded. Later advice ("не передавай пароль") never
    # vetoes an otherwise useful explanation. Never shorten an atomic item.
    claims_success = _SUCCESS.search(" ".join(selected) if selected else units[0])
    promoted = False
    if claims_success:
        failures = [unit for unit in units[consumed:] if _is_failure(unit)] + hidden_outcomes
        for failure in failures:
            finished = _finish(failure)
            if finished in selected:
                continue
            if len(finished) > budget:
                # A contradictory outcome cannot be conveyed coherently.
                return _NOTICE
            while selected and (len(selected) >= 3 or
                                len(" ".join([*selected, finished])) > budget):
                # Drop the generic acknowledgement first; keep concrete counts
                # and outcomes when space allows.
                generic = next((i for i, item in enumerate(selected)
                                if re.fullmatch(r"(?:готово|done)[.!]", item, re.I)), None)
                selected.pop(generic if generic is not None else 0)
            selected.append(finished)
            promoted = True
            if len(selected) == 3:
                break
    if not selected:
        return _NOTICE
    spoken = " ".join(selected)
    if consumed < len(units) or removed or promoted or clause_tail:
        return spoken + " " + _NOTICE
    return spoken
