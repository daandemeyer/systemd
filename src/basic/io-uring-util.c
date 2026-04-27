/* SPDX-License-Identifier: LGPL-2.1-or-later */

#include "io-uring-util.h"
#include "log.h" /* IWYU pragma: keep */

#if HAVE_LIBURING
DLSYM_PROTOTYPE(io_uring_queue_init_params) = NULL;
DLSYM_PROTOTYPE(io_uring_queue_exit) = NULL;
DLSYM_PROTOTYPE(io_uring_submit) = NULL;
#endif

int dlopen_io_uring(int log_level) {
#if HAVE_LIBURING
        static void *io_uring_dl = NULL;

        LIBURING_NOTE(suggested);

        return dlopen_many_sym_or_warn(
                        &io_uring_dl,
                        "liburing.so.2", log_level,
                        DLSYM_ARG(io_uring_queue_init_params),
                        DLSYM_ARG(io_uring_queue_exit),
                        DLSYM_ARG(io_uring_submit));
#else
        return log_full_errno(log_level, SYNTHETIC_ERRNO(EOPNOTSUPP),
                              "liburing support is not compiled in.");
#endif
}
