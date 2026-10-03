# Hot cache entries are evicted first

With a capacity-2 cache: put a, put b, read a, put c. I expected `b` to be evicted since `a` was just used, but `a` is gone and `b` is still there. Reading an entry should count as a use.
