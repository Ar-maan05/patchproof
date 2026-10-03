# Slug validation accepts names with spaces

`is_valid_slug("hello world")` returns True and the page is then created with a broken URL. Same for `"hello-"`. Slugs must be lowercase alphanumerics separated by single hyphens, with nothing else.
