"""HLS WebVTT segment merger module."""

import pathlib
import re
import typing
from collections.abc import Sequence

from .errors import MalformedCaptionError, MalformedSegmentError, WebVTTError
from .models import Caption, Region, Style, Timestamp
from .utils import _Path
from .webvtt import WebVTT

_T = typing.TypeVar('_T')

# MPEG-TS timestamps have 33 bits
_MAX_MPEGTS = 2 ** 33 - 1
# the MPEG-TS clock of WebVTT timestamps, 90 kHz, so ticks in a millisecond
_TICKS_PER_MILLISECOND = 90

# the keys of an X-TIMESTAMP-MAP header, in either order
_TIMESTAMP_MAP_PATTERN = re.compile(r'X-TIMESTAMP-MAP=(.*)')
_FIELD_PATTERN = re.compile(r'(LOCAL|MPEGTS):(.*)')

# a path to a segment, or the captions of a segment already read
_Segment = _Path | WebVTT


def merge_segments(
        segments: Sequence[_Segment],
        *,
        strict: bool = True
        ) -> WebVTT:
    """
    Merge the ordered WebVTT segments of an HLS playlist into one file.

    Each segment is read, unless it is a `WebVTT` instance, and the local
    times of its cues are mapped to the playlist timeline with its
    ``X-TIMESTAMP-MAP`` header: the MPEG-TS value unwrapped across the
    33-bit wraparounds of the MPEG-TS clock, plus the LOCAL time of the
    segment. Cues that adjacent segments repeat at the same time and with
    the same content are kept once, and the styles and regions of the
    segments are merged, keeping the first of each. The result is a
    `WebVTT` instance that can be saved or read back, and the inputs are
    not changed.

    :param segments: the paths of the segment files, in playlist order, or
        `WebVTT` instances of their captions
    :param strict: read the segments with strict parsing, so a malformed
        cue or other invalid block raises instead of being skipped
    :returns: the captions of the playlist as a `WebVTT` instance

    :raises MalformedSegmentError: if a segment has no X-TIMESTAMP-MAP
        header, one that is not valid or more than one, a timeline that is
        not after the previous segment, a cue mapped to a negative time, a
        malformed cue or a cue identifier repeated in the segment, or if a
        segment file cannot be read as WebVTT
    """
    merged_captions: list[Caption] = []
    styles: list[Style] = []
    regions: list[Region] = []
    timeline = _Timeline()

    for number, segment_file in enumerate(segments, 1):
        captions_file = _read_segment(segment_file, number, strict)
        base = timeline.base_of(captions_file.metadata, number)

        _check_duplicate_identifiers(captions_file.captions, number)

        for caption in captions_file.captions:
            merged = _mapped_caption(caption, base, number)
            if merged not in merged_captions:
                merged_captions.append(merged)

        _merge_items(styles, captions_file.styles)
        _merge_items(regions, captions_file.regions)

    return WebVTT(captions=merged_captions,
                  styles=styles,
                  regions=regions)


class _Timeline:
    """The playlist timeline, unwrapping the 33-bit MPEG-TS clock."""

    def __init__(self) -> None:
        """Initialize at the start of the playlist."""
        self.previous_base: int | None = None
        self.previous_mpegts: int | None = None
        self.rollovers = 0

    def base_of(self, metadata: Sequence[str], number: int) -> int:
        """
        Return the offset of a segment on the unwrapped clock, in ticks.

        :param metadata: the metadata lines of the header of the segment
        :param number: the number of the segment, from 1
        :returns: the offset of the start of the segment, in ticks

        :raises MalformedSegmentError: if the map of the segment is missing
            or not valid, or its timeline is not after the previous one
        """
        local, mpegts = _timestamp_map(metadata, number)

        if self.previous_mpegts is not None and mpegts < self.previous_mpegts:
            self.rollovers += 1
        self.previous_mpegts = mpegts

        base = ((mpegts + self.rollovers * (_MAX_MPEGTS + 1)) -
                local * _TICKS_PER_MILLISECOND)
        if self.previous_base is not None and base < self.previous_base:
            raise MalformedSegmentError(
                number,
                f'timeline is not after the timeline of segment {number - 1}'
                )
        self.previous_base = base
        return base


def _merge_items(items: list[_T], segment_items: Sequence[_T]) -> None:
    """
    Append the items of a segment that are not already merged.

    :param items: the items merged so far, kept in first-seen order
    :param segment_items: the items of the segment
    """
    for item in segment_items:
        if item not in items:
            items.append(item)


def merge_folder(
        folder: _Path,
        *,
        pattern: str = r'fileSequence\d+\.webvtt',
        strict: bool = True
        ) -> WebVTT:
    """
    Merge the WebVTT segment files of a folder in their playlist order.

    The files whose name matches ``pattern`` are merged as with
    `merge_segments`, in the order of their sequence numbers when the
    pattern captures them as in ``fileSequenceN.webvtt``, or else in name
    order. Files that do not match, such as the playlist, are left out.

    :param folder: the folder with the segment files
    :param pattern: the regular expression of the names of the segments,
        whose first capturing group is their sequence number when present
    :param strict: read the segments with strict parsing, so a malformed
        cue or other invalid block raises instead of being skipped
    :returns: the captions of the playlist as a `WebVTT` instance

    :raises FileNotFoundError: if the folder has no file matching pattern
    :raises MalformedSegmentError: as `merge_segments`
    """
    folder_path = pathlib.Path(folder)
    paths = sorted(folder_path.iterdir(),
                   key=lambda path: _segment_order(path.name, pattern))
    segments = [path for path in paths
                if path.is_file() and re.fullmatch(pattern, path.name)]
    if not segments:
        raise FileNotFoundError(
            f'No segment matching {pattern!r} in {str(folder_path)!r}'
            )
    return merge_segments(segments, strict=strict)


def _read_segment(
        segment: _Segment,
        number: int,
        strict: bool
        ) -> WebVTT:
    """
    Read a segment, or return the captions of one already read.

    A copy is returned, so mapping the cues of the result leaves a
    `WebVTT` instance given as an input unchanged.

    :param segment: a path to a segment file or a `WebVTT` instance
    :param number: the number of the segment, from 1, for error messages
    :param strict: read file segments with strict parsing
    :returns: the captions of the segment

    :raises MalformedSegmentError: if the file cannot be read as WebVTT
    """
    if isinstance(segment, WebVTT):
        return segment.copy()
    try:
        return WebVTT.read(segment, strict=strict)
    except WebVTTError as error:
        raise MalformedSegmentError(number, str(error)) from error


def _timestamp_map(
        metadata: Sequence[str],
        number: int
        ) -> tuple[int, int]:
    """
    Return the LOCAL time and MPEG-TS value of the map of a segment.

    :param metadata: the metadata lines of the header of the segment
    :param number: the number of the segment, from 1
    :returns: the LOCAL time in milliseconds and the MPEG-TS value in ticks

    :raises MalformedSegmentError: if the map is missing, repeated or not
        valid
    """
    maps = [line for line in metadata
            if _TIMESTAMP_MAP_PATTERN.fullmatch(line)]
    if not maps:
        raise MalformedSegmentError(
            number, 'missing X-TIMESTAMP-MAP header'
            )
    if len(maps) > 1:
        raise MalformedSegmentError(
            number,
            f'conflicting X-TIMESTAMP-MAP headers: {maps!r}'
            )

    fields: dict[str, str] = {}
    for field in maps[0][len('X-TIMESTAMP-MAP='):].split(','):
        match = _FIELD_PATTERN.fullmatch(field)
        if match is None or match.group(1) in fields:
            raise MalformedSegmentError(
                number, f'invalid X-TIMESTAMP-MAP header {maps[0]!r}'
                )
        fields[match.group(1)] = match.group(2)

    if set(fields) != {'LOCAL', 'MPEGTS'}:
        raise MalformedSegmentError(
            number, f'invalid X-TIMESTAMP-MAP header {maps[0]!r}'
            )
    return _local_time(fields['LOCAL'], number, maps[0]), \
        _mpegts_value(fields['MPEGTS'], number, maps[0])


def _local_time(value: str, number: int, header: str) -> int:
    """
    Return the LOCAL time of an X-TIMESTAMP-MAP header in milliseconds.

    :raises MalformedSegmentError: if the time is not valid
    """
    try:
        return Timestamp.from_string(value).in_milliseconds()
    except MalformedCaptionError:
        raise MalformedSegmentError(
            number, f'invalid X-TIMESTAMP-MAP header {header!r}'
            ) from None


def _mpegts_value(value: str, number: int, header: str) -> int:
    """
    Return the MPEG-TS value of an X-TIMESTAMP-MAP header.

    :raises MalformedSegmentError: if the value is not a whole number from
        0 to 2**33 - 1
    """
    if not re.fullmatch(r'[0-9]+', value):
        raise MalformedSegmentError(
            number, f'invalid X-TIMESTAMP-MAP header {header!r}'
            )
    mpegts = int(value)
    if mpegts > _MAX_MPEGTS:
        raise MalformedSegmentError(
            number, f'invalid X-TIMESTAMP-MAP header {header!r}'
            )
    return mpegts


def _check_duplicate_identifiers(
        captions: Sequence[Caption],
        number: int
        ) -> None:
    """
    Check that no cue identifier of a segment is repeated.

    Cues without an identifier are not checked, as WebVTT identifies a cue
    by the line before its timings only when it has one.

    :raises MalformedSegmentError: if two cues have the same identifier
    """
    identifiers: set[str] = set()
    for caption in captions:
        identifier = caption.identifier
        if identifier is not None:
            if identifier in identifiers:
                raise MalformedSegmentError(
                    number, f'duplicate cue identifier {identifier!r}'
                    )
            identifiers.add(identifier)


def _mapped_caption(
        caption: Caption,
        base: int,
        number: int
        ) -> Caption:
    """
    Return a copy of a cue with its times mapped to the playlist timeline.

    :param caption: the cue of the segment
    :param base: the offset of the start of the segment, in MPEG-TS ticks
    :param number: the number of the segment, from 1
    :returns: a new cue on the playlist timeline

    :raises MalformedSegmentError: if a time is mapped to a negative time
        or after the largest timestamp
    """
    mapped = caption.copy()
    for name, local in (('start', caption.start_in_milliseconds),
                        ('end', caption.end_in_milliseconds)):
        ticks = base + local * _TICKS_PER_MILLISECOND
        if ticks < 0:
            raise MalformedSegmentError(
                number,
                f'cue {name} time maps to a negative time: {ticks} ticks'
                )
        try:
            milliseconds = _ticks_to_milliseconds(ticks)
            timestamp = Timestamp._from_milliseconds(milliseconds)
        except ValueError:
            raise MalformedSegmentError(
                number,
                f'cue {name} time maps after the largest timestamp'
                ) from None
        if name == 'start':
            mapped.start_time = timestamp
        else:
            mapped.end_time = timestamp
    return mapped


def _ticks_to_milliseconds(ticks: int) -> int:
    """
    Return MPEG-TS ticks in milliseconds, rounded to the nearest one.

    Halves are rounded away from zero, which for non-negative ticks rounds
    up, as the timestamps of the rest of the package.

    :param ticks: a number of ticks of the 90 kHz clock
    :returns: the number of milliseconds
    """
    quotient, remainder = divmod(abs(ticks), _TICKS_PER_MILLISECOND)
    quotient += 2 * remainder >= _TICKS_PER_MILLISECOND
    return quotient if ticks >= 0 else -quotient


def _segment_order(name: str, pattern: str) -> tuple[int, typing.Any]:
    """
    Return the order key of a segment file name.

    A name whose pattern has a capturing group of a whole number, as in
    ``fileSequenceN.webvtt``, is ordered by it, and the others by name, so
    numbered and unnumbered names are never in the same numbered order.

    :param name: the name of the file
    :param pattern: the regular expression of the names of the segments
    :returns: a key with 0 and the number or 1 and the name
    """
    match = re.fullmatch(pattern, name)
    if match is not None and match.groups():
        return (0, int(match.group(1)))
    return (1, name)
