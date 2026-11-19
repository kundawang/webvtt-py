"""Errors module."""


class WebVTTError(Exception):
    """
    Base class of the errors of webvtt-py.

    Catching it catches every error defined in this module.
    """


class MalformedFileError(WebVTTError, ValueError):
    """
    File is not in the right format.

    Raised when reading captions whose content is not valid for the
    expected format (WebVTT, SRT or SBV), e.g. a WebVTT file missing the
    ``WEBVTT`` header.
    """


class MalformedCaptionError(WebVTTError, ValueError):
    """
    Caption not in the right format.

    Raised when a timestamp is not valid, e.g. when creating a
    :class:`~webvtt.models.Caption`, setting its ``start`` or ``end`` or
    reading a file that contains a cue with an invalid timestamp. Also
    raised when a cue setting is not valid, e.g. when setting one of
    :class:`~webvtt.models.CueSettings`.
    """


class MalformedRegionError(MalformedCaptionError):
    """
    Region not in the right format.

    Raised when a region setting is not valid, e.g. when creating a
    :class:`~webvtt.models.Region` or setting one of its settings, or when
    reading a file with strict parsing that contains a region with a
    setting that is not valid or a region after the first cue. It is a
    subclass of :class:`MalformedCaptionError`, so catching that error
    also catches it.
    """


class MalformedSegmentError(WebVTTError, ValueError):
    """
    HLS WebVTT segment not in the right format for merging.

    Raised when merging the WebVTT segments of an HLS playlist, e.g. for a
    segment without an ``X-TIMESTAMP-MAP`` header, with one that is not
    valid or conflicts with another, a segment whose timeline is not after
    the previous one, a cue mapped to a negative time, a malformed cue or a
    repeated cue identifier. The :attr:`segment` is the number of the
    segment in the order given, from 1, and every message starts with it.
    """

    def __init__(self, segment: int, message: str):
        """
        Initialize.

        :param segment: the number of the segment, from 1
        :param message: the description of the problem
        """
        self.segment = segment
        super().__init__(f'segment {segment}: {message}')


class MissingFilenameError(WebVTTError):
    """
    Missing a filename when saving to disk.

    Raised when saving captions without an output path and the
    :class:`~webvtt.webvtt.WebVTT` instance was not read from a file.
    """
