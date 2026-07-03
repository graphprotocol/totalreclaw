"""Tests for the heuristic query-entity extractor (entity-trapdoor read-side).

These tests pin the behavior of the Python port of the validated TypeScript
spike `extractEntities()` (totalreclaw-internal/e2e/retrieval-benchmark/
entity-trapdoor.ts). The extractor is pure-stdlib, deterministic, and must
match the spike's offline collision results (Task 0 asymmetry gate, N=150).
"""

from totalreclaw.entity_extract import extract_query_entities


class TestExtractQueryEntities:
    def test_multi_word_capitalized_phrase(self):
        result = extract_query_entities("I bought a Honda Civic last week")
        assert "honda civic" in result

    def test_single_caps_word_mid_sentence(self):
        result = extract_query_entities("my trip to Lisbon was great")
        assert "lisbon" in result

    def test_sentence_start_caps_excluded(self):
        result = extract_query_entities("The meeting was useful")
        assert "the" not in result

    def test_acronym(self):
        result = extract_query_entities("deploy to AWS tonight")
        assert "aws" in result

    def test_domain_seed_lowercase(self):
        result = extract_query_entities("rotate the wifi password")
        assert "wifi" in result

    def test_empty_string(self):
        assert extract_query_entities("") == []

    def test_whitespace_only(self):
        assert extract_query_entities("   ") == []

    def test_dedup_and_normalize_collapse_ws(self):
        # "Node  JS": "Node" is sentence-start (rule 2 excluded); "JS" is an
        # acronym ([A-Z]{2,5}) not a lowercased-tail word, so no multi-cap
        # phrase "node js" is formed. "JS" -> "js" via the acronym rule.
        # "Node.js" -> full non-alphanumeric strip -> "Nodejs" -> single-cap
        # rule -> "nodejs" (interior punctuation stripped, matching the spike).
        result = extract_query_entities("Node  JS and Node.js")
        assert "js" in result
        assert "nodejs" in result
        assert "node js" not in result

    def test_punctuation_and_numbers_no_crash(self):
        result = extract_query_entities("123 !!! ...")
        assert isinstance(result, list)

    def test_possessive_mid_sentence_full_strip(self):
        # Mid-sentence possessive (NOT sentence start, so rule 2 applies).
        # The JS spike strips ALL non-alphanumerics (``/[^A-Za-z0-9]/g``), so
        # "Michael's" -> "Michaels" -> passes ^[A-Z][a-z]{2,}$ -> "michaels".
        # NOTE: this still does NOT collide with an LLM-write entity "michael"
        # (different surface form) -- the canonicalizer lever (TODO #370) would
        # bridge that; banked fallback, not built now. (A sentence-start
        # "Michael's ..." is excluded by rule 2 and yields nothing, which is why
        # this test uses a mid-sentence position.)
        result = extract_query_entities("I saw Michael's car yesterday")
        assert "michaels" in result
