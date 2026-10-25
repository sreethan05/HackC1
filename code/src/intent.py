"""Deletion-intent classification for request_text.

The corpus contains requests in many languages. Deterministic multilingual
keyword rules classify the clear cases; the optional LLM layer (llm.py) is
consulted only when the rules have no confident verdict, so unseen phrasings
still classify correctly.

Deletion-authorized: erase/delete/remove/close account + personal data.
NOT deletion (-> NOT_APPLICABLE): access/withdrawal-of-consent-only, export or
download of data, correction of data, password/login help, marketing opt-out
only, invoice copy, email-address change, general questions.
"""
from __future__ import annotations

import re

# Verified deletion intents ( stems shown; matched case-insensitively )
_DELETE_MARKERS = [
    # english
    "permanent account deletion", "permanently delete", "permanent deletion",
    "delete my account", "delete the account", "deletion of the account",
    "delete this account", "erase the account", "account erased", "erase my",
    "remove my account", "remove the account", "remove all personal",
    "close my account", "close this account", "close the account",
    "account closed and", "deletion request", "process the associated personal-data deletion",
    "personal-data deletion", "delete my personal", "remove my personal",
    "have my account deleted", "want my data deleted", "wipe my",
    # spanish / portuguese / french / german / italian / dutch / romanian / swahili...
    "eliminación permanente", "eliminacion permanente", "exclusão permanente",
    "exclusao permanente", "suppression définitive", "suppression definitive",
    "löschung meines kontos", "konto endgültig löschen", "cancellazione definitiva",
    "definitieve verwijdering", "stergerea definitiva", "ștergerea definitivă",
    "kufuta konto", " wiping ", "borrar mi cuenta", "apagar minha conta",
    "supprimer mon compte", "mein konto löschen", "cancellare il mio account",
    # japanese / korean / chinese (templates verified in-corpus)
    "完全な削除を依頼", "アカウントの完全な削除", "永続的な削除", "削除をリクエスト",
    "영구 삭제를 요청", "계정의 영구 삭제", "永久删除", "注销并删除",
    # additional verified templates
    "begin the deletion process", "deletion process for the account",
    "to be erased", "account to be erased",
    # audit-style deletion confirmations (ask the agent to prove the deletion)
    "is gone", "was deleted", "had been deleted", "already deleted",
    "cannot identify me", "no longer identifiable", "cannot be identified",
]

# generic deletion language: any deletion verb plus an account/data reference
# somewhere in the request (covers indirect phrasings such as "verify that my
# account was deleted" or "remove the records tied to both addresses")
_DELETE_GENERIC = re.compile(
    r"(delet|erasur|erase|remov|clos|purge|destroy|wipe|suppress|elimin|exclu|"
    r"l\u00f6sch|supprim|削除|삭제|删除|borrar|apagar|supprimer)", re.I)
_ACCOUNT_NOUN = re.compile(
    r"(account|cuenta|conta|compte|konto|profile|data|information|records|"
    r"アカウント|계정|个人资料)", re.I)

# Non-deletion intents (any hit => not authorized, unless a delete marker also applies)
_NON_DELETE_MARKERS = [
    "stop the promotional emails", "remove me from marketing lists",
    "download my account information", "portable format", "copy of the information",
    "what personal information you hold", "information request",
    "misspelled", "correct it without removing", "resetting the login",
    "can't access my profile", "cannot access my profile", "reset my password",
    "copy of last year's invoices", "leave my profile",
    "change the email address used", "keep the account",
    "unsubscribe", "opt out of marketing", "opt-out of marketing",
    "export my data", "data portability",
]

# Explicit keep/repair instructions override any deletion-flavoured wording
_STRONG_KEEP_MARKERS = [
    "do not close", "don't close", "do not delete", "don't delete",
    "do not remove", "don't remove", "keep the account", "keep my account",
    "keep my profile", "still need the account", "leave my profile",
    "without removing", "do not close the account",
]


def classify(request_text: str, llm_fallback=None) -> tuple[str, str]:
    """Return (AUTHORIZED | NOT_DELETION, reason). llm_fallback(text)->bool is
    consulted only when no rule fires."""
    # normalize typographic apostrophes/quotes before matching
    text = (request_text or "").lower()
    for curly in ("\u2019", "\u2018", "\u02bc"):
        text = text.replace(curly, "'")
    if any(m in text for m in _STRONG_KEEP_MARKERS):
        return "NOT_DELETION", "explicit keep/repair instruction"
    non = [m for m in _NON_DELETE_MARKERS if m in text]
    dele = [m for m in _DELETE_MARKERS if m in text]
    if dele and not non:
        return "AUTHORIZED", f"deletion marker: {dele[0]}"
    if not non and _DELETE_GENERIC.search(text) and _ACCOUNT_NOUN.search(text):
        return "AUTHORIZED", "generic deletion language"
    if non and not dele:
        return "NOT_DELETION", f"non-deletion marker: {non[0]}"
    if dele and non:
        # both families matched: deletion language wins only if it targets the account
        if re.search(r"(delete|erase|remove|close|suppress|elimin|exclus|l\u00f6sch|削除|삭제)", text) \
                and re.search(r"(account|cuenta|conta|compte|konto|アカウント|계정)", text):
            return "AUTHORIZED", "account deletion with extra asks"
        return "NOT_DELETION", "ambiguous; non-deletion asks dominate"
    if llm_fallback is not None:
        verdict = bool(llm_fallback(request_text))
        return ("AUTHORIZED", "llm fallback") if verdict else ("NOT_DELETION", "llm fallback")
    return "NOT_DELETION", "no deletion signal found"
