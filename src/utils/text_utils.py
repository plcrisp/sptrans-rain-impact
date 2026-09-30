import difflib
import re
import unicodedata


def normalize_text(text: str) -> str:
    """Normaliza texto para comparação de destinos e terminais."""
    if not text:
        return ""
    text = unicodedata.normalize("NFKD", str(text)).encode("ASCII", "ignore").decode("utf-8")
    text = text.upper()
    replacements = {
        "PCA.": "PRACA",
        "PCA": "PRACA",
        "TERM.": "TERMINAL",
        "TERM": "TERMINAL",
        "METRO": "METRO",
        "EST.": "ESTACAO",
        "JD.": "JARDIM",
        "VL.": "VILA",
        "PQ.": "PARQUE",
        "AV.": "AVENIDA",
        "R.": "RUA",
    }
    tokens = re.findall(r"\w+", text)
    return " ".join([replacements.get(t, t) for t in tokens])


def text_similarity(s1: str, s2: str) -> float:
    """Calcula similaridade textual combinando Jaccard e SequenceMatcher."""
    n1 = normalize_text(s1)
    n2 = normalize_text(s2)
    if not n1 or not n2:
        return 0.0
    t1 = set(n1.split())
    t2 = set(n2.split())
    jaccard = len(t1 & t2) / len(t1 | t2) if (t1 | t2) else 0.0
    seq = difflib.SequenceMatcher(None, n1, n2).ratio()
    return max(jaccard, seq)
