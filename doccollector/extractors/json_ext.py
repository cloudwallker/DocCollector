from .txt import TxtExtractor


class JsonExtractor(TxtExtractor):
    # This round searches the raw file text; \uXXXX escapes are not unescaped
    # before matching.
    file_type = "json"
