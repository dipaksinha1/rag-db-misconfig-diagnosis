"""``DiagnosisError`` lives here so ``pgdiag.llm`` can subclass it without importing core."""


class DiagnosisError(RuntimeError):
    """The model did not produce valid output after all attempts, or the API call failed."""
