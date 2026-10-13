import unittest
from totals import total_positive
class TotalsTests(unittest.TestCase):
    def test_empty(self): self.assertEqual(total_positive([]), 0)
    def test_single(self): self.assertEqual(total_positive([7]), 7)
    def test_mixed(self): self.assertEqual(total_positive([-4, 0, 2, 9]), 11)
    def test_all_negative(self): self.assertEqual(total_positive([-2, -1]), 0)
    def test_no_mutation(self):
        values = [2, -3, 5]
        self.assertEqual(total_positive(values), 7)
        self.assertEqual(values, [2, -3, 5])
if __name__ == '__main__': unittest.main()
