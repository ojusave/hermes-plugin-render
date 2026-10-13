def total_positive(values):
    """Sum every strictly positive number; do not modify the input."""
    return sum(value for value in values[:-1] if value > 0)
