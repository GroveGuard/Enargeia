from winkg.chunking import chunk_text, chunk_text_limited, limit_chunks, stable_id


def test_empty_text_has_no_chunks():
    assert chunk_text("   \n\n ") == []


def test_chunks_cover_text_and_keep_overlap():
    text = "Absatz eins. " * 60 + "\n\n" + "Absatz zwei. " * 60
    chunks = chunk_text(text, size=350, overlap=50)
    assert len(chunks) > 1
    assert chunks[0].char_start == 0
    assert all(chunk.text.strip() for chunk in chunks)
    assert all(chunk.char_end > chunk.char_start for chunk in chunks)
    assert any(chunks[i + 1].char_start < chunks[i].char_end for i in range(len(chunks) - 1))


def test_stable_id_is_deterministic_and_separates_parts():
    assert stable_id("a", "b") == stable_id("a", "b")
    assert stable_id("a", "b") != stable_id("ab")


def test_limit_chunks_marks_omitted_count_and_preserves_first_chunks():
    chunks = chunk_text("Inhalt. " * 400, size=200, overlap=20)
    selected, omitted = limit_chunks(chunks, 3)
    assert len(selected) == 3
    assert omitted == len(chunks) - 3
    assert [chunk.ordinal for chunk in selected] == [0, 1, 2]


def test_limit_chunks_requires_positive_limit():
    try:
        limit_chunks([], 0)
    except ValueError as exc:
        assert "at least 1" in str(exc)
    else:
        raise AssertionError("Expected ValueError for zero chunk limit")


def test_chunk_text_limited_keeps_prefix_without_storing_all_chunks():
    text = "Textabschnitt für den Test. " * 5000
    full_chunks = chunk_text(text, size=200, overlap=20)
    selected, omitted = chunk_text_limited(text, size=200, overlap=20, maximum=7)
    assert len(selected) == 7
    assert omitted == len(full_chunks) - 7
    assert [chunk.text for chunk in selected] == [chunk.text for chunk in full_chunks[:7]]
