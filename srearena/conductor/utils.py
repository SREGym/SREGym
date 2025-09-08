def is_ordered_subset(A: list, B: list) -> bool:
    """Check if list A is a subset of B and in the same order."""
    it = iter(B)
    return all(a in it for a in A)
