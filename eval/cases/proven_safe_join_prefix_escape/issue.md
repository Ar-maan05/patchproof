# Static handler serves files outside the document root

With the root set to `/srv/data`, a request for `../data-secret/x` is served from `/srv/data-secret/x`. Paths that resolve outside the root must be rejected with ValueError, including sibling directories whose names merely start with the root's name.
