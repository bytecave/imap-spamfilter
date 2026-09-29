bind_socket = "*:11333";
# rspamd 4.2 lets a client ask this worker to read a file or shared-memory
# segment on rspamd's own disk instead of sending the message. Every
# container on spamnet can reach this port without a password, and the
# filter always sends message bytes, so switch the feature off.
allow_file_and_shm_inputs = false;
