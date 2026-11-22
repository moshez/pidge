"""Tests for the small leaf modules: JSON narrowing, ULIDs and the model."""

import unittest

from hamcrest import assert_that, contains_exactly, equal_to, instance_of

from .. import jsonvalue, ulid
from ..model import Direction, Meta, SystemTag, meta_json, parse_meta, tag_name
from .checks import failure


class TestJSONValue(unittest.TestCase):
    """Narrowing untyped JSON."""

    def test_narrowing(self) -> None:
        """Values of the right shape pass through."""
        data = jsonvalue.loads_dict('{"a": [{"n": "x"}], "s": "t", "k": 2}')
        assert_that(jsonvalue.pluck(data, "a", "n"), contains_exactly("x"))
        assert_that(jsonvalue.get_str(data, "s"), equal_to("t"))
        assert_that(jsonvalue.get_str(data, "missing", "d"), equal_to("d"))
        assert_that(jsonvalue.next_count(data, "k"), equal_to(3))
        assert_that(jsonvalue.get_dict(data, "missing"), equal_to({}))
        assert_that(jsonvalue.as_list(("a",)), equal_to(["a"]))

    def test_wrong_shapes(self) -> None:
        """Values of the wrong shape raise JSONShapeError."""
        error = instance_of(jsonvalue.JSONShapeError)
        assert_that(failure(lambda: jsonvalue.as_dict([])), error)
        assert_that(failure(lambda: jsonvalue.as_list("x")), error)
        assert_that(failure(lambda: jsonvalue.as_str(1)), error)
        assert_that(failure(lambda: jsonvalue.get_number({"n": True}, "n")), error)
        assert_that(failure(lambda: None), equal_to(None))

    def test_dumps_is_sorted(self) -> None:
        """Serialization is compact and sorted."""
        assert_that(jsonvalue.dumps({"b": 1, "a": 2}), equal_to(b'{"a":2,"b":1}'))

    def test_error_details(self) -> None:
        """Botocore-style errors expose their Error member."""
        error = ValueError("x")
        setattr(error, "response", {"Error": {"Code": "Nope"}})
        assert_that(jsonvalue.error_details(error), equal_to({"Code": "Nope"}))
        assert_that(jsonvalue.error_details(ValueError()), equal_to({}))


class TestULID(unittest.TestCase):
    """ULID encoding."""

    def test_round_trip(self) -> None:
        """The timestamp survives encoding, and IDs sort by time."""
        early = ulid.encode(1_000, 5)
        late = ulid.encode(2_000, 1)
        assert_that(ulid.timestamp(early), equal_to(1.0))
        assert_that(early < late, equal_to(True))
        assert_that(ulid.is_ulid(early), equal_to(True))
        assert_that(ulid.is_ulid("not-a-ulid"), equal_to(False))

    def test_new(self) -> None:
        """New IDs use the clock and the random source."""
        made = ulid.new_ulid(lambda: 3.0, lambda bits: bits)
        assert_that(made, equal_to(ulid.encode(3000, 80)))


class TestModel(unittest.TestCase):
    """The meta record."""

    def test_round_trip(self) -> None:
        """Meta records serialize and parse back, with sorted unique tags."""
        meta = Meta(tags=("b", "a"), direction=Direction.OUT, status="sent")
        parsed = parse_meta(meta_json(meta))
        assert_that(parsed.direction, equal_to(Direction.OUT))
        assert_that(parse_meta(b'{"tags": ["z", "z", "y"]}').tags, equal_to(("y", "z")))

    def test_tag_names(self) -> None:
        """System tags have kebab-case names."""
        assert_that(tag_name(SystemTag.INGEST_FAILED), equal_to("ingest-failed"))
