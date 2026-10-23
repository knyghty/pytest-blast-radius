"""Break code in many ways and check selection runs every test that fails."""

import pytest

from tests.conftest import Project

FILES = {
    "shop/__init__.py": "",
    "shop/constants.py": "VAT = 21\nCURRENCY = 'EUR'\n",
    "shop/prices.py": """\
        import functools

        from shop import constants


        def with_vat(amount):
            return amount * (100 + constants.VAT) // 100


        @functools.cache
        def rates():
            return {"EUR": 1, "USD": 2}


        def convert(amount, currency):
            return amount * rates()[currency]
        """,
    "shop/models.py": """\
        from shop import prices


        class Product:
            discount = 0

            def __init__(self, name, price):
                self.name = name
                self.price = price

            def total(self):
                return prices.with_vat(self.price - self.discount)


        class Sale(Product):
            discount = 10
        """,
    "shop/catalogue.py": """\
        from pathlib import Path

        from shop.models import Product


        def load():
            lines = (Path(__file__).parent / "products.txt").read_text().splitlines()
            return [Product(name, int(price)) for name, price in map(str.split, lines)]


        def names():
            return sorted((product.name for product in load()))
        """,
    "shop/products.txt": "apple 100\npear 200\n",
    "tests/test_prices.py": """\
        from shop import prices


        def test_with_vat():
            assert prices.with_vat(100) == 121


        def test_convert():
            assert prices.convert(10, "USD") == 20


        def test_convert_euros():
            assert prices.convert(10, "EUR") == 10
        """,
    "tests/test_models.py": """\
        from shop.models import Product, Sale


        def test_total():
            assert Product("a", 100).total() == 121


        def test_sale_total():
            assert Sale("a", 110).total() == 121
        """,
    "tests/test_catalogue.py": """\
        import pytest

        from shop import catalogue


        @pytest.fixture(scope="module")
        def products():
            return catalogue.load()


        def test_count(products):
            assert len(products) == 2


        def test_first(products):
            assert products[0].name == "apple"


        def test_names():
            assert catalogue.names() == ["apple", "pear"]
        """,
}
MUTATIONS = [
    ("shop/constants.py", "VAT = 21", "VAT = 9"),
    ("shop/prices.py", "100 + constants.VAT", "100 - constants.VAT"),
    ("shop/prices.py", '"USD": 2', '"USD": 3'),
    ("shop/prices.py", "rates()[currency]", "rates()[currency] + 1"),
    ("shop/models.py", "discount = 10", "discount = 20"),
    ("shop/models.py", "discount = 0", "discount = 1"),
    ("shop/models.py", "self.price = price", "self.price = price + 1"),
    ("shop/catalogue.py", "in load())", "in load()), reverse=True"),
    ("shop/catalogue.py", "Product(name,", "Product(name.upper(),"),
    ("shop/products.txt", "pear 200", "pear 200\nplum 300"),
    ("shop/products.txt", "apple", "banana"),
]


@pytest.mark.parametrize(("path", "old", "new"), MUTATIONS)
def test_selection_runs_every_failing_test(
    project: Project, path: str, old: str, new: str
) -> None:
    project.commit(FILES)
    project.run("--blast-radius").assert_outcomes(passed=8)
    source = (project.path / path).read_text()
    assert old in source
    project.write({path: source.replace(old, new)})
    result = project.run("--blast-radius-check")
    assert result.parseoutcomes().get("failed", 0) > 0
    result.stdout.fnmatch_lines(["selection would have run every failing test *"])
