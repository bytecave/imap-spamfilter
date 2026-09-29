# Nothing here uses rspamd's proxy worker (*:11332), but the stock image
# still starts it, reachable from spamnet without a password. Keep it from
# reading files or shared memory on rspamd's disk for a client.
allow_file_and_shm_inputs = false;
