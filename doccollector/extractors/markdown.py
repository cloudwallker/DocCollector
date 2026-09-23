from .txt import TxtExtractor


class MarkdownExtractor(TxtExtractor):
    file_type = "md"
