"""
End-to-end example: using Enzyme in an agent's generation loop.

This shows the chain from accumulated content → conceptual index →
preference retrieval → generation context. The agent doesn't need to
write precise queries because Enzyme's index already encodes the
patterns specific to each entity.

Requires: enzyme binary on PATH, with an indexed vault.
"""

import sys
sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent.parent))

from enzyme_sdk import EnzymeClient, Collection, Document


def main():
    client = EnzymeClient()

    # --- Step 1: Bootstrap on existing content ---
    # Point at a vault that's already been indexed with `enzyme init`.
    # This could be a user's reading highlights, design sessions, meeting
    # notes — anything that accumulates over time.

    vault_path = sys.argv[1] if len(sys.argv) > 1 else "~/vault/content/Readwise"
    coll = Collection("reading", client=client, vault_path=vault_path)

    print("=== What the index understands ===\n")

    # `overview()` shows the entities Enzyme tracks and the thematic
    # questions it generated for each. These questions are how search
    # gets routed — they encode what's interesting about each entity's
    # documents without you having to specify it.

    overview = coll.overview(top=5)
    print(f"{overview.total_entities} entities tracked\n")

    for entity in overview.entities[:3]:
        print(f"  {entity.name} ({entity.entity_type}, {entity.frequency} docs)")
        # Each entity has questions that characterize its content.
        # These were generated from the documents, not written by hand.
        if entity.catalysts:
            q = entity.catalysts[0]
            text = q["text"] if isinstance(q, dict) else q
            print(f"    → {text[:90]}...")
        print()

    # --- Step 2: Agent searches for preference context ---
    # The agent is about to generate something (a design concept,
    # a recommendation, a summary). It needs context from the user's
    # history. The query can be broad — the index handles routing.

    print("=== Search: conceptual retrieval ===\n")

    query = "deliberate curation and editorial taste vs algorithmic recommendation"
    print(f"  Query: \"{query}\"\n")

    results = coll.search(query, limit=5)

    for r in results.results[:3]:
        # Full documents, not fragments. The agent gets the complete
        # context — decisions, quotes, rationale — in one retrieval.
        print(f"  [{r.similarity:.3f}] {r.file_path}")
        # Show first meaningful line
        lines = [l.strip() for l in r.content.split("\n") if l.strip() and len(l.strip()) > 20]
        if lines:
            print(f"    \"{lines[0][:100]}...\"")
        print()

    # The contributing catalysts tell the agent WHY these documents
    # were surfaced. This is the routing signal — it explains the
    # conceptual connection between the query and the results.

    if results.top_contributing_catalysts:
        print("  Routed via:")
        for c in results.top_contributing_catalysts[:2]:
            print(f"    [{c.entity}] \"{c.text[:80]}...\"")
        print()

    # --- Step 3: Use the results in a generation prompt ---
    # The agent now has:
    # - Ranked documents with full prose (not fragments)
    # - The thematic questions that explain why each was retrieved
    # - The entity associations (which person/topic/tag it belongs to)
    #
    # It injects this into the generation prompt as context. The
    # questions tell it what patterns to focus on; the document prose
    # gives it the specific language and reasoning to draw from.

    print("=== Generation context (what goes into the prompt) ===\n")

    if results.results:
        top = results.results[0]
        print(f"  Top result: {top.file_path}")
        print(f"  Similarity: {top.similarity:.3f}")
        print(f"  Content length: {len(top.content)} chars")
        print()

        if results.top_contributing_catalysts:
            c = results.top_contributing_catalysts[0]
            print(f"  The index says this is relevant because:")
            print(f"    \"{c.text}\"")
            print()
            print(f"  The agent uses this signal to focus its generation —")
            print(f"  not just 'here are some related documents' but")
            print(f"  'here's the specific pattern worth attending to.'")

    # --- Step 4: Add new content, index evolves ---
    # After the session, the agent writes a new document. On next
    # refresh, Enzyme updates its questions to incorporate the new
    # content. The index gets sharper over time.

    print("\n\n=== Adding new content ===\n")

    doc = Document.from_text(
        "Session — editorial taste discussion",
        "Discussed the difference between algorithmic and curatorial "
        "recommendation. Key insight: the value isn't in accuracy but in "
        "legibility of reasoning. The curator can articulate *why* you "
        "should pay attention — the algorithm can only show you what's similar.",
        tags=["editorial", "curation", "ecology-of-technology"],
        links=["Maggie Appleton"],
    )

    print(f"  Would write: {doc.filename()}")
    print(f"  Tags: {doc.tags}")
    print(f"  Entities: {doc.links}")
    print(f"  After refresh, new questions form around these entities.")
    print(f"  Next search for 'editorial taste' will be sharper because")
    print(f"  the index now has more content to characterize the pattern.")


if __name__ == "__main__":
    main()
