import pytest
from conftest import SAMPLE_LATIN1, SAMPLE_PNG, Topology

from pubgate.filtering import check_conflict_markers, check_residual_markers, is_ignored, scrub_internal_blocks


class TestScrubInternalBlocks:
    def test_hash_markers(self):
        content = "public\n# BEGIN-INTERNAL\nsecret\n# END-INTERNAL\nmore public\n"
        assert scrub_internal_blocks(content) == "public\nmore public\n"

    def test_slash_markers(self):
        content = "code();\n// BEGIN-INTERNAL\nint secret = 1;\n// END-INTERNAL\ncode2();\n"
        assert scrub_internal_blocks(content) == "code();\ncode2();\n"

    def test_html_markers(self):
        content = "<p>public</p>\n<!-- BEGIN-INTERNAL -->\n<p>secret</p>\n<!-- END-INTERNAL -->\n<p>more</p>\n"
        assert scrub_internal_blocks(content) == "<p>public</p>\n<p>more</p>\n"

    def test_no_markers(self):
        content = "just regular content\nno secrets here\n"
        assert scrub_internal_blocks(content) == content

    def test_unclosed_marker(self):
        content = "public\n# BEGIN-INTERNAL\nsecret stays hidden\n"
        with pytest.raises(ValueError, match="unclosed BEGIN-INTERNAL at line 2"):
            scrub_internal_blocks(content, path="file.txt")

    def test_orphan_end_marker(self):
        content = "public\n# END-INTERNAL\nmore\n"
        with pytest.raises(ValueError, match="orphan END-INTERNAL at line 2"):
            scrub_internal_blocks(content, path="file.txt")

    def test_nested_begin_marker(self):
        content = "# BEGIN-INTERNAL\n# BEGIN-INTERNAL\n# END-INTERNAL\n"
        with pytest.raises(ValueError, match="nested BEGIN-INTERNAL at line 2"):
            scrub_internal_blocks(content, path="file.txt")

    def test_relaxed_hash_no_space(self):
        content = "public\n#BEGIN-INTERNAL\nsecret\n#END-INTERNAL\nmore\n"
        assert scrub_internal_blocks(content) == "public\nmore\n"

    def test_relaxed_hash_extra_spaces(self):
        content = "public\n#  BEGIN-INTERNAL\nsecret\n#  END-INTERNAL\nmore\n"
        assert scrub_internal_blocks(content) == "public\nmore\n"

    def test_relaxed_slash_no_space(self):
        content = "code();\n//BEGIN-INTERNAL\nint secret;\n//END-INTERNAL\ncode2();\n"
        assert scrub_internal_blocks(content) == "code();\ncode2();\n"

    def test_relaxed_html_extra_spaces(self):
        content = "<p>ok</p>\n<!--  BEGIN-INTERNAL  -->\n<p>secret</p>\n<!--  END-INTERNAL  -->\n<p>ok2</p>\n"
        assert scrub_internal_blocks(content) == "<p>ok</p>\n<p>ok2</p>\n"

    def test_multiple_blocks(self):
        content = "a\n# BEGIN-INTERNAL\nb\n# END-INTERNAL\nc\n# BEGIN-INTERNAL\nd\n# END-INTERNAL\ne\n"
        assert scrub_internal_blocks(content) == "a\nc\ne\n"

    def test_empty_content(self):
        assert scrub_internal_blocks("") == ""

    def test_marker_at_start_of_file(self):
        content = "# BEGIN-INTERNAL\nsecret\n# END-INTERNAL\npublic line\n"
        assert scrub_internal_blocks(content) == "public line\n"

    def test_marker_at_end_no_trailing_newline(self):
        content = "public line\n# BEGIN-INTERNAL\nsecret\n# END-INTERNAL"
        assert scrub_internal_blocks(content) == "public line\n"

    def test_entire_file_is_internal_block(self):
        content = "# BEGIN-INTERNAL\nall secret\nmore secret\n# END-INTERNAL\n"
        assert scrub_internal_blocks(content) == ""


class TestIsIgnored:
    def test_glob_pattern(self):
        assert is_ignored(".internal/secrets.txt", [".internal/*"])
        assert not is_ignored("src/app.py", [".internal/*"])

    def test_basename_pattern(self):
        assert is_ignored("path/to/file.secret", ["*.secret"])
        assert not is_ignored("path/to/file.py", ["*.secret"])

    def test_suffix_pattern(self):
        assert is_ignored("notes-internal.md", ["*-internal.*"])
        assert not is_ignored("notes-public.md", ["*-internal.*"])

    def test_no_patterns(self):
        assert not is_ignored("any-file.txt", [])

    def test_multiple_patterns(self):
        patterns = [".internal/*", "*.secret", "*-internal.*"]
        assert is_ignored(".internal/foo", patterns)
        assert is_ignored("key.secret", patterns)
        assert is_ignored("doc-internal.md", patterns)
        assert not is_ignored("README.md", patterns)


class TestIsBinaryAtRef:
    def test_binary_detected(self, topo: Topology):
        topo.commit_internal({"image.png": SAMPLE_PNG})
        assert topo.work_dir.git.is_binary_at_ref("HEAD", "image.png")

    def test_text_not_binary(self, topo: Topology):
        assert not topo.work_dir.git.is_binary_at_ref("HEAD", "file1.txt")

    def test_missing_file(self, topo: Topology):
        assert not topo.work_dir.git.is_binary_at_ref("HEAD", "no-such-file.bin")

    def test_non_utf8_detected_as_binary(self, topo: Topology):
        topo.commit_internal({"latin1.txt": SAMPLE_LATIN1})
        assert topo.work_dir.git.is_binary_at_ref("HEAD", "latin1.txt")


class TestCheckResidualMarkers:
    def test_clean_content_passes(self):
        check_residual_markers("no markers here\njust text\n", "file.txt")

    def test_residual_begin_marker(self):
        content = "public\n# BEGIN-INTERNAL\n"
        with pytest.raises(ValueError, match="residual marker.*line 2"):
            check_residual_markers(content, "file.txt")

    def test_residual_end_marker(self):
        content = "public\n# END-INTERNAL\n"
        with pytest.raises(ValueError, match="residual marker.*line 2"):
            check_residual_markers(content, "file.txt")


class TestCheckConflictMarkers:
    def test_clean_content_passes(self):
        check_conflict_markers("no markers here\njust text\n", "file.txt")

    def test_detects_left_marker(self):
        content = "line1\n<<<<<<< HEAD\nours\n=======\ntheirs\n>>>>>>> branch\n"
        with pytest.raises(ValueError, match="unresolved merge-conflict marker at line 2"):
            check_conflict_markers(content, "file.txt")

    def test_detects_equals_marker(self):
        content = "line1\n=======\n"
        with pytest.raises(ValueError, match="unresolved merge-conflict marker at line 2"):
            check_conflict_markers(content, "file.txt")

    def test_detects_right_marker(self):
        content = "line1\n>>>>>>> branch\n"
        with pytest.raises(ValueError, match="unresolved merge-conflict marker at line 2"):
            check_conflict_markers(content, "file.txt")

    def test_seven_chars_in_middle_of_line_ignored(self):
        # Conflict markers must be at the start of a line
        content = "text <<<<<<< not a marker\n"
        check_conflict_markers(content, "file.txt")
