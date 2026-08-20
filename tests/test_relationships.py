from simulation.relationships import clamp_relationship, update_relationship
from database.models import Relationship


def test_relationship_is_clamped_to_valid_range():
    relationship = Relationship(from_npc_id=1, to_npc_id=2, score=99)
    update_relationship(relationship, 20)
    assert relationship.score == 100
    update_relationship(relationship, -250)
    assert relationship.score == -100
    assert clamp_relationship(1000) == 100

