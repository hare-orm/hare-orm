from tests.shared_models.testmodels import Author, Book


class QueryShapeRows:
    """Fixed book graphs created inside each test's existing cache-verification scope."""

    @staticmethod
    async def create_three_rated_books() -> Author:
        """Create alpha/beta/gamma rated 1/2/3 and return their author to retain its lifetime."""
        author = await QueryShapeRows.create_two_rated_books()
        await Book.objects.create(name="gamma", author=author, rating=3.0)
        return author

    @staticmethod
    async def create_books_by_two_authors() -> tuple[Author, Author]:
        """Create alpha/beta for two authors and return both to retain their lifetimes."""
        author_one = await Author.objects.create(name="a1")
        author_two = await Author.objects.create(name="a2")
        await Book.objects.create(name="alpha", author=author_one, rating=1.0)
        await Book.objects.create(name="beta", author=author_two, rating=2.0)
        return author_one, author_two

    @staticmethod
    async def create_two_rated_books(beta_rating: float = 2.0) -> Author:
        """Create alpha rated 1.0 and beta at the supplied rating, retaining their author."""
        author = await Author.objects.create(name="a")
        await Book.objects.create(name="alpha", author=author, rating=1.0)
        await Book.objects.create(name="beta", author=author, rating=beta_rating)
        return author
