# Use the in-stack Unbound for recursive DNS so DNSBL lookups are not
# rate-limited by a shared resolver. Critical for Spamhaus to work.
dns {
    nameserver = ["spamfilter-unbound"];
    timeout = 1s;
    sockets = 16;
    retransmits = 5;
}
