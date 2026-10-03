# ip_in_cidr says 10.0.0.5 is not in 10.0.0.0/24

Allow-list checks reject addresses that are plainly inside the configured network:

    ip_in_cidr("10.0.0.5", "10.0.0.0/24")  # False

Expected True. Addresses outside the network (e.g. 10.0.1.5) should still be False, and /32 should match exactly one host.
