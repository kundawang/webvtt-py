import os
import pathlib
import shutil
import tempfile
import textwrap
import unittest

import webvtt
from webvtt import merger
from webvtt.errors import MalformedSegmentError, WebVTTError

PATH_TO_FIXTURES = (
    pathlib.Path(__file__).resolve().parent / 'samples' / 'hls_merge'
    )

# the offsets on the playlist timeline of the three fixture segments, in ms
SEGMENT_BASES = (10_000, 95_443_717, 95_443_718)


def segment(
        mpegts,
        local='00:00:00.000',
        *,
        local_first=False,
        metadata=(),
        captions=(),
        styles=(),
        regions=()
        ):
    """
    Build a WebVTT instance of an HLS segment in memory.

    :param mpegts: the MPEG-TS value of its timestamp map
    :param local: the LOCAL time of its timestamp map
    :param local_first: write LOCAL before MPEGTS in the map
    :param metadata: extra metadata lines of the header
    :param captions: the lines of the cue blocks of the segment
    :param styles: the text of the STYLE blocks
    :param regions: the lines of the REGION blocks
    :returns: the `webvtt.WebVTT` instance
    """
    map_line = (
        f'X-TIMESTAMP-MAP=LOCAL:{local},MPEGTS:{mpegts}' if local_first
        else f'X-TIMESTAMP-MAP=MPEGTS:{mpegts},LOCAL:{local}'
        )
    content = ['WEBVTT', map_line, *metadata]
    for region in regions:
        content.extend(['', 'REGION', *region])
    for style in styles:
        content.extend(['', 'STYLE', *style])
    for block in captions:
        content.extend(['', *block])
    return webvtt.from_string('\n'.join(content))


def write_segments(folder, segments):
    """
    Write segment instances to files named fileSequenceN.webvtt.

    :param folder: the destination folder
    :param segments: the segments, in order
    :returns: the list of paths
    """
    paths = []
    for index, captions in enumerate(segments):
        path = pathlib.Path(folder) / f'fileSequence{index}.webvtt'
        captions.save(path)
        paths.append(path)
    return paths


class TestMergeSegments(unittest.TestCase):

    def test_maps_local_times_to_the_playlist_timeline(self):
        segments = [
            segment(900_000, captions=[
                ['00:00:00.500 --> 00:00:01.500', 'first'],
                ['00:00:02.000 --> 00:00:03.000', 'second'],
                ]),
            segment(1_800_000, captions=[
                ['00:00:00.250 --> 00:00:01.250', 'third'],
                ]),
            ]

        merged = merger.merge_segments(segments)

        self.assertEqual(
            [(c.start, c.end, c.text) for c in merged.captions],
            [('00:00:10.500', '00:00:11.500', 'first'),
             ('00:00:12.000', '00:00:13.000', 'second'),
             ('00:00:20.250', '00:00:21.250', 'third')]
            )

    def test_map_with_local_offset(self):
        segments = [
            segment(900_000, local='00:00:01.000', captions=[
                ['00:00:00.500 --> 00:00:01.500', 'first'],
                ]),
            # the same map with LOCAL written before MPEGTS
            segment(900_000, local='00:00:01.000', local_first=True,
                    captions=[
                        ['00:00:02.000 --> 00:00:03.000', 'second'],
                        ]),
            ]

        merged = merger.merge_segments(segments)

        self.assertEqual(
            [(c.start, c.end) for c in merged.captions],
            [('00:00:09.500', '00:00:10.500'),
             ('00:00:11.000', '00:00:12.000')]
            )

    def test_unwraps_the_33_bit_mpegts_clock(self):
        wrap = 2 ** 33
        segments = [
            segment(wrap - 90_000, captions=[
                ['00:00:00.000 --> 00:00:01.000', 'before'],
                ]),
            segment(90_000, captions=[
                ['00:00:00.000 --> 00:00:01.000', 'after'],
                ]),
            ]

        merged = merger.merge_segments(segments)

        # both bases are exact milliseconds, 95,442,718 s and two seconds on
        self.assertEqual(
            [(c.start, c.end, c.text) for c in merged.captions],
            [('26:30:42.718', '26:30:43.718', 'before'),
             ('26:30:44.718', '26:30:45.718', 'after')]
            )

    def test_keeps_cue_identifier_settings_style_and_region(self):
        segments = [
            segment(900_000,
                    styles=[['::cue(.yellow) {', '  color: yellow;', '}']],
                    regions=[['id:fred', 'width:40%']],
                    captions=[
                        ['intro',
                         '00:00:00.500 --> 00:00:01.500 '
                         'align:start region:fred',
                         '<c.yellow>Alpha</c>'],
                        ]),
            segment(1_800_000,
                    styles=[['::cue(.yellow) {', '  color: yellow;', '}']],
                    regions=[['id:fred', 'width:40%']],
                    captions=[
                        ['00:00:00.250 --> 00:00:01.250', 'plain'],
                        ]),
            ]

        merged = merger.merge_segments(segments)

        self.assertEqual(len(merged.styles), 1)
        self.assertEqual(len(merged.regions), 1)
        self.assertEqual(merged.regions[0].id, 'fred')
        first = merged.captions[0]
        self.assertEqual(first.identifier, 'intro')
        self.assertEqual(str(first.settings), 'align:start region:fred')
        self.assertEqual(first.raw_text, '<c.yellow>Alpha</c>')
        # the output keeps the style, region, identifier and settings
        self.assertEqual(
            merged.content,
            textwrap.dedent('''
                WEBVTT

                REGION
                id:fred
                width:40%

                STYLE
                ::cue(.yellow) {
                  color: yellow;
                }

                intro
                00:00:10.500 --> 00:00:11.500 align:start region:fred
                <c.yellow>Alpha</c>

                00:00:20.250 --> 00:00:21.250
                plain
                ''').lstrip()
            )

    def test_deduplicates_the_overlapping_cues_of_adjacent_segments(self):
        captions = [
            ['00:00:01.500 --> 00:00:02.500', 'same'],
            ['00:00:03.000 --> 00:00:04.000', 'only here'],
            ]
        segments = [
            segment(900_000, captions=captions),
            # the first cue is repeated at the same time on the playlist
            # timeline, the second one is not, as a boundary can fall in a
            # cue; another cue starts the next segment
            segment(1_800_000, local='00:00:10.000', captions=[
                ['00:00:01.500 --> 00:00:02.500', 'same'],
                ['00:00:00.500 --> 00:00:01.000', 'next segment'],
                ]),
            ]

        merged = merger.merge_segments(segments)

        self.assertEqual(
            [(c.start, c.end, c.text) for c in merged.captions],
            [('00:00:11.500', '00:00:12.500', 'same'),
             ('00:00:13.000', '00:00:14.000', 'only here'),
             ('00:00:10.500', '00:00:11.000', 'next segment')]
            )

    def test_deduplication_compares_settings_and_text(self):
        segments = [
            segment(900_000, captions=[
                ['00:00:01.000 --> 00:00:02.000 align:start', 'text'],
                ]),
            segment(1_800_000, captions=[
                # same time on the timeline, different settings and text
                ['00:00:01.000 --> 00:00:02.000', 'other text'],
                # same time and text, different settings
                ['00:00:02.000 --> 00:00:03.000 align:end', 'text'],
                ]),
            ]

        merged = merger.merge_segments(segments)

        self.assertEqual(len(merged.captions), 3)

    def test_merges_distinct_styles_and_regions_once_each(self):
        segments = [
            segment(900_000,
                    styles=[['::cue(a) { color: red; }']],
                    regions=[['id:one']]),
            segment(1_800_000,
                    styles=[['::cue(a) { color: red; }'],
                            ['::cue(b) { color: blue; }']],
                    regions=[['id:one'], ['id:two']]),
            ]

        merged = merger.merge_segments(segments)

        self.assertEqual(
            [style.text for style in merged.styles],
            ['::cue(a) { color: red; }', '::cue(b) { color: blue; }']
            )
        self.assertEqual([region.id for region in merged.regions],
                         ['one', 'two'])

    def test_does_not_change_the_input_segments(self):
        segments = [
            segment(900_000, captions=[
                ['00:00:00.500 --> 00:00:01.500', 'first'],
                ]),
            segment(1_800_000, captions=[
                ['00:00:01.500 --> 00:00:02.500', 'second'],
                ]),
            ]
        before = [vtt.content for vtt in segments]

        merger.merge_segments(segments)

        self.assertEqual([vtt.content for vtt in segments], before)
        self.assertEqual(
            [(c.start, c.end) for c in segments[0].captions],
            [('00:00:00.500', '00:00:01.500')]
            )

    def test_merged_cues_are_copies(self):
        segments = [segment(900_000, captions=[
            ['00:00:00.500 --> 00:00:01.500', 'first'],
            ])]

        merged = merger.merge_segments(segments)
        merged.captions[0].text = 'changed'

        self.assertEqual(segments[0].captions[0].text, 'first')

    def test_merge_no_segments(self):
        merged = merger.merge_segments([])

        self.assertEqual(merged.captions, [])
        self.assertEqual(merged.styles, [])
        self.assertEqual(merged.regions, [])
        self.assertEqual(merged.content, 'WEBVTT\n')

    def test_rounds_sub_millisecond_ticks(self):
        # MPEGTS 28 ticks is not a whole number of milliseconds
        segments = [
            segment(28, captions=[
                ['00:00:00.000 --> 00:00:01.000', 'first'],
                ]),
            segment(118, captions=[
                ['00:00:00.000 --> 00:00:01.000', 'second'],
                ]),
            ]

        merged = merger.merge_segments(segments)

        # 28/90 rounds to 0 ms and the first cue ends at 118 ticks, 1 ms;
        # the second base 118 ticks rounds to 1 ms and its cue ends at 208
        # ticks, 2 ms
        self.assertEqual(
            [(c.start, c.end) for c in merged.captions],
            [('00:00:00.000', '00:00:01.000'),
             ('00:00:00.001', '00:00:01.001')]
            )
        # a value below half a millisecond rounds down to zero
        self.assertEqual(merger._ticks_to_milliseconds(44), 0)
        self.assertEqual(merger._ticks_to_milliseconds(45), 1)


class TestMergeFolder(unittest.TestCase):

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.folder = pathlib.Path(self.temp_dir.name)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_merges_the_files_of_a_folder_in_sequence_order(self):
        write_segments(self.folder, [
            segment(900_000, captions=[
                ['00:00:00.500 --> 00:00:01.500', 'first'],
                ]),
            segment(1_800_000, captions=[
                ['00:00:00.500 --> 00:00:01.500', 'second'],
                ]),
            ])
        (self.folder / 'prog_index.m3u8').write_text('#EXTM3U\n')
        # a file with a larger name but smaller number must not sort first
        shutil.copy(self.folder / 'fileSequence0.webvtt',
                    self.folder / 'fileSequence10.webvtt.bak')

        merged = merger.merge_folder(self.folder)

        self.assertEqual(
            [c.text for c in merged.captions], ['first', 'second']
            )

    def test_merges_with_a_custom_pattern(self):
        for index, mpegts in enumerate((900_000, 1_800_000)):
            (self.folder / f'part-{index}.vtt').write_text(
                segment(mpegts, captions=[
                    ['00:00:00.500 --> 00:00:01.500', f'cue {index}'],
                    ]).content
                )

        merged = merger.merge_folder(self.folder, pattern=r'part-\d+\.vtt')

        self.assertEqual([c.text for c in merged.captions],
                         ['cue 0', 'cue 1'])

    def test_folder_without_a_segment(self):
        with self.assertRaises(FileNotFoundError) as cm:
            merger.merge_folder(self.folder)

        self.assertIn('No segment', str(cm.exception))

    def test_merges_the_three_fixture_segments(self):
        merged = merger.merge_folder(PATH_TO_FIXTURES)
        base0, base1, base2 = SEGMENT_BASES

        # five cues, in segment order: the Charlie cue of the last segment
        # is the repeated one, so it is not added again
        self.assertEqual(
            [(c.identifier, c.start, c.end, c.raw_text)
             for c in merged.captions],
            [('alpha',
              _at(base0, 500), _at(base0, 1500), '<c.yellow>Alpha</c>'),
             (None, _at(base0, 1500), _at(base0, 2500), 'Bravo'),
             (None, _at(base1, 1500), _at(base1, 2500), 'Bravo'),
             ('charlie', _at(base1, 2500), _at(base1, 3500), 'Charlie'),
             (None, _at(base2, 1211), _at(base2, 2011), 'Delta')]
            )
        # the styles and regions of the segments are kept once
        self.assertEqual(len(merged.styles), 1)
        self.assertEqual(len(merged.regions), 1)
        self.assertEqual(merged.regions[0].id, 'fred')
        # the identifiers and settings survive
        self.assertEqual(
            str(merged.captions[0].settings), 'align:start region:fred'
            )
        self.assertEqual(str(merged.captions[3].settings), 'line:0')
        # the merged file can be read back
        self.assertEqual(webvtt.from_string(merged.content), merged)

    def test_fixtures_use_a_wraparound_and_repeat_a_cue(self):
        # documents what the fixture exercises: one 33-bit wraparound and an
        # overlapping cue, deduplicated by merge_folder
        mpegts_values = []
        for path in sorted(PATH_TO_FIXTURES.iterdir()):
            metadata = webvtt.read(path).metadata
            map_line = next(line for line in metadata
                            if line.startswith('X-TIMESTAMP-MAP'))
            fields = dict(
                field.partition(':')[::2]
                for field in map_line[len('X-TIMESTAMP-MAP='):].split(',')
                )
            mpegts_values.append(int(fields['MPEGTS']))

        self.assertEqual(mpegts_values, [900_000, 8589934530, 90028])
        self.assertGreater(mpegts_values[0], mpegts_values[2])

        raw = sum(len(webvtt.read(path).captions)
                  for path in PATH_TO_FIXTURES.iterdir())
        self.assertEqual(raw, 6)
        self.assertEqual(len(merger.merge_folder(PATH_TO_FIXTURES)), 5)


def _at(base, milliseconds):
    """Return the timestamp string base + milliseconds."""
    total = base + milliseconds
    hours, remainder = divmod(total, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    seconds, milliseconds = divmod(remainder, 1000)
    return f'{hours:02d}:{minutes:02d}:{seconds:02d}.{milliseconds:03d}'


class TestMergeSegmentErrors(unittest.TestCase):

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.temp_dir.cleanup()

    def assert_segment_error(self, segments, number, message):
        """Assert merge_segments raises a segment error with the message."""
        with self.assertRaises(MalformedSegmentError) as cm:
            merger.merge_segments(segments)

        self.assertEqual(cm.exception.segment, number)
        self.assertEqual(str(cm.exception), f'segment {number}: {message}')

    def test_error_is_a_webvtt_error_and_a_value_error(self):
        self.assertTrue(issubclass(MalformedSegmentError, WebVTTError))
        self.assertTrue(issubclass(MalformedSegmentError, ValueError))

    def test_missing_timestamp_map(self):
        captions = webvtt.WebVTT(captions=[
            webvtt.Caption('00:00:00.500', '00:00:01.500', 'first')
            ])
        self.assert_segment_error(
            [captions], 1, 'missing X-TIMESTAMP-MAP header'
            )

    def test_conflicting_timestamp_maps(self):
        captions = segment(900_000, metadata=[
            'X-TIMESTAMP-MAP=MPEGTS:100,LOCAL:00:00:00.000'
            ])
        self.assert_segment_error(
            [captions], 1,
            f"conflicting X-TIMESTAMP-MAP headers: {captions.metadata!r}"
            )

    def test_invalid_timestamp_map(self):
        base = segment(900_000)
        for map_line in (
                'X-TIMESTAMP-MAP=MPEGTS:900000',
                'X-TIMESTAMP-MAP=MPEGTS:900000,LOCAL:00:00:00.000,EXTRA:1',
                'X-TIMESTAMP-MAP=MPEGTS:abc,LOCAL:00:00:00.000',
                'X-TIMESTAMP-MAP=MPEGTS:900000,LOCAL:not-a-time',
                f'X-TIMESTAMP-MAP=MPEGTS:{2 ** 33},LOCAL:00:00:00.000',
                'X-TIMESTAMP-MAP=MPEGTS:-1,LOCAL:00:00:00.000',
                'X-TIMESTAMP-MAP=MPEGTS:900000,MPEGTS:100',
                ):
            with self.subTest(map_line=map_line):
                captions = webvtt.WebVTT(metadata=[map_line])
                with self.assertRaises(MalformedSegmentError) as cm:
                    merger.merge_segments([base, captions])

                self.assertEqual(cm.exception.segment, 2)
        self.assertEqual(
            str(cm.exception),
            'segment 2: invalid X-TIMESTAMP-MAP header '
            + repr(map_line)
            )

    def test_non_monotonic_segment_without_wraparound(self):
        segments = [
            segment(1_800_000),
            # a LOCAL offset moves the start before the previous segment
            segment(1_800_000, local='00:00:01.000'),
            ]
        self.assert_segment_error(
            segments, 2,
            'timeline is not after the timeline of segment 1'
            )

    def test_non_monotonic_segment_with_wraparound(self):
        wrap = 2 ** 33
        segments = [
            segment(wrap - 90_000),
            # after a wraparound, the LOCAL offset moves the start before
            # the previous segment
            segment(90_000, local='00:00:05.000'),
            ]
        self.assert_segment_error(
            segments, 2,
            'timeline is not after the timeline of segment 1'
            )

    def test_negative_mapped_time(self):
        segments = [
            segment(90_000, local='00:00:02.000', captions=[
                ['00:00:00.500 --> 00:00:01.500', 'first'],
                ]),
            ]
        # base is -1000 ms, so the cue starts at -500 ms
        self.assert_segment_error(
            segments, 1, 'cue start time maps to a negative time: -45000 ticks'
            )

    def test_negative_mapped_end_time(self):
        segments = [
            segment(45_000, local='00:00:01.000', captions=[
                ['00:00:00.500 --> 00:00:00.499', 'first'],
                ]),
            ]
        # the base is -500 ms: the start maps to zero, the end to -1 ms
        self.assert_segment_error(
            segments, 1, 'cue end time maps to a negative time: -90 ticks'
            )

    def test_time_mapped_after_the_largest_timestamp(self):
        # the largest timestamp is 9999999999:59:59.999; a base past it is
        # reached with many 33-bit wraparounds of the MPEG-TS clock
        wrap = 2 ** 33
        segments = [
            segment(wrap - 1, captions=[
                ['26:30:44.000 --> 26:30:45.000', 'first'],
                ]),
            segment(90_000, captions=[
                ['00:00:00.000 --> 00:00:01.000', 'second'],
                ]),
            ]

        # one wraparound stays within the range of a timestamp
        merged = merger.merge_segments(segments)
        self.assertEqual(len(merged.captions), 2)

        # the helper rejects the values the largest timestamp cannot hold
        caption = webvtt.Caption('00:00:01.000', '00:00:02.000', 'first')
        with self.assertRaises(MalformedSegmentError) as cm:
            merger._mapped_caption(
                caption, (10 ** 10 * 3_600_000) * 90, 1
                )

        self.assertEqual(cm.exception.segment, 1)
        self.assertEqual(
            str(cm.exception),
            'segment 1: cue start time maps after the largest timestamp'
            )

    def test_duplicate_identifier(self):
        captions = segment(900_000, captions=[
            ['same', '00:00:00.500 --> 00:00:01.500', 'first'],
            ['same', '00:00:02.000 --> 00:00:03.000', 'second'],
            ])
        self.assert_segment_error(
            [captions], 1, "duplicate cue identifier 'same'"
            )

    def test_identifier_repeated_in_another_segment_is_not_an_error(self):
        segments = [
            segment(900_000, captions=[
                ['same', '00:00:00.500 --> 00:00:01.500', 'first'],
                ]),
            segment(1_800_000, captions=[
                ['same', '00:00:00.500 --> 00:00:01.500', 'second'],
                ]),
            ]

        merged = merger.merge_segments(segments)

        self.assertEqual([c.identifier for c in merged.captions],
                         ['same', 'same'])

    def test_malformed_cue_with_strict_parsing(self):
        path = pathlib.Path(self.temp_dir.name) / 'broken.webvtt'
        path.write_text(textwrap.dedent('''
            WEBVTT
            X-TIMESTAMP-MAP=MPEGTS:900000,LOCAL:00:00:00.000

            00:00:01.000 --> not-a-time
            broken
            ''').lstrip())

        with self.assertRaises(MalformedSegmentError) as cm:
            merger.merge_segments([path])

        self.assertEqual(cm.exception.segment, 1)
        self.assertTrue(str(cm.exception).startswith('segment 1: line '))

    def test_malformed_cue_skipped_without_strict_parsing(self):
        path = pathlib.Path(self.temp_dir.name) / 'broken.webvtt'
        path.write_text(textwrap.dedent('''
            WEBVTT
            X-TIMESTAMP-MAP=MPEGTS:900000,LOCAL:00:00:00.000

            00:00:01.000 --> not-a-time
            broken

            00:00:01.000 --> 00:00:02.000
            kept
            ''').lstrip())

        merged = merger.merge_segments([path], strict=False)

        self.assertEqual([c.text for c in merged.captions], ['kept'])

    def test_file_that_is_not_webvtt(self):
        path = pathlib.Path(self.temp_dir.name) / 'not-vtt.webvtt'
        path.write_text('not a WebVTT file\n')

        with self.assertRaises(MalformedSegmentError) as cm:
            merger.merge_segments([path])

        self.assertEqual(cm.exception.segment, 1)

    def test_segment_number_counts_from_one(self):
        segments = [segment(900_000), segment(1_800_000),
                    webvtt.WebVTT()]
        with self.assertRaises(MalformedSegmentError) as cm:
            merger.merge_segments(segments)

        self.assertEqual(cm.exception.segment, 3)

    def test_merging_paths_matches_merging_instances(self):
        paths = write_segments(self.temp_dir.name, [
            segment(900_000, captions=[
                ['00:00:00.500 --> 00:00:01.500', 'first'],
                ]),
            segment(1_800_000, captions=[
                ['00:00:00.500 --> 00:00:01.500', 'second'],
                ]),
            ])

        from_files = merger.merge_segments(paths)
        from_instances = merger.merge_segments(
            [webvtt.read(path) for path in paths]
            )

        self.assertEqual(from_files, from_instances)

    def test_merge_folder_ignores_folders(self):
        write_segments(self.temp_dir.name, [
            segment(900_000, captions=[
                ['00:00:00.500 --> 00:00:01.500', 'first'],
                ]),
            ])
        os.mkdir(self.temp_dir.name + '/fileSequence9.webvtt')

        merged = merger.merge_folder(self.temp_dir.name)

        self.assertEqual([c.text for c in merged.captions], ['first'])

    def test_segment_order_keys(self):
        pattern = r'fileSequence(\d+)\.webvtt'
        self.assertEqual(
            merger._segment_order('fileSequence10.webvtt', pattern), (0, 10)
            )
        self.assertEqual(
            merger._segment_order('other.webvtt', pattern),
            (1, 'other.webvtt')
            )
        # a pattern without a capturing group orders the names
        self.assertEqual(
            merger._segment_order('part-1.vtt', r'part-\d+\.vtt'),
            (1, 'part-1.vtt')
            )

    def test_round_trip_of_the_segmenter(self):
        source = webvtt.WebVTT(captions=[
            webvtt.Caption('00:00:00.500', '00:00:12.000', 'spanning'),
            webvtt.Caption('00:00:15.000', '00:00:18.000', 'second'),
            ])
        webvtt.segment(source, self.temp_dir.name, seconds=10)

        merged = merger.merge_folder(self.temp_dir.name)

        # the cue spanning two segments is merged back into one, with the
        # times of the original captions
        self.assertEqual(
            [(c.start, c.end, c.text) for c in merged.captions],
            [('00:00:10.500', '00:00:22.000', 'spanning'),
             ('00:00:25.000', '00:00:28.000', 'second')]
            )

    def test_unwraps_more_than_one_33_bit_rollover(self):
        wrap = 2 ** 33
        values = (wrap - 45, 45, 90, wrap - 90)
        segments = [
            segment(mpegts, captions=[
                ['00:00:00.000 --> 00:00:01.000', f'cue {index}'],
                ])
            for index, mpegts in enumerate(values)
            ]

        merged = merger.merge_segments(segments)

        # the first three bases are consecutive milliseconds, the last one
        # adds another 33-bit wraparound, ~26.5 h later
        self.assertEqual(
            [c.start for c in merged.captions[:3]],
            ['26:30:43.717', '26:30:43.718', '26:30:43.719']
            )
        self.assertEqual(merged.captions[3].start, '53:01:27.434')


if __name__ == '__main__':
    unittest.main()
