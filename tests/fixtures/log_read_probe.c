/* Count actual libc file-read bytes in installed browser processes.
 * LD_PRELOAD is confined to the resource-test subprocess and its children.
 * There are no substitutions for gwflow code or filesystem contents.
 */
#define _GNU_SOURCE
#include <dlfcn.h>
#include <fcntl.h>
#include <limits.h>
#include <stdatomic.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/types.h>
#include <unistd.h>

static ssize_t (*original_read)(int, void *, size_t);
static ssize_t (*original_pread)(int, void *, size_t, off_t);
static ssize_t (*original_pread64)(int, void *, size_t, off64_t);
static _Atomic unsigned long long total;

__attribute__((constructor)) static void initialize(void) {
    original_read = dlsym(RTLD_NEXT, "read");
    original_pread = dlsym(RTLD_NEXT, "pread");
    original_pread64 = dlsym(RTLD_NEXT, "pread64");
}

static void wait_before_read(int fd) {
    const char *selected = getenv("GWFLOW_LOG_READ_PATH");
    const char *gate = getenv("GWFLOW_LOG_READ_GATE");
    const char *started = getenv("GWFLOW_LOG_READ_STARTED");
    if (!selected || !gate || !started) return;
    char descriptor[64], path[PATH_MAX];
    snprintf(descriptor, sizeof descriptor, "/proc/self/fd/%d", fd);
    ssize_t length = readlink(descriptor, path, sizeof path - 1);
    if (length < 0) return;
    path[length] = '\0';
    if (strcmp(path, selected)) return;
    int marker = open(started, O_WRONLY | O_CREAT | O_TRUNC, 0600);
    if (marker >= 0) {
        char pid[64];
        int count = snprintf(pid, sizeof pid, "%ld\n", (long)getpid());
        ssize_t written = write(marker, pid, (size_t)count);
        (void)written;
        close(marker);
    }
    while (access(gate, F_OK) == 0) usleep(10000);
}

static void count_bytes(int fd, ssize_t count) {
    if (count <= 0) return;
    char descriptor[64], path[PATH_MAX];
    snprintf(descriptor, sizeof descriptor, "/proc/self/fd/%d", fd);
    ssize_t length = readlink(descriptor, path, sizeof path - 1);
    if (length < 0) return;
    path[length] = '\0';
    if (strstr(path, "/gwflow-log-view-") && strstr(path, "/exact.stdout"))
        atomic_fetch_add(&total, (unsigned long long)count);
}

ssize_t read(int fd, void *buffer, size_t count) {
    wait_before_read(fd);
    ssize_t result = original_read(fd, buffer, count);
    count_bytes(fd, result);
    return result;
}

ssize_t pread(int fd, void *buffer, size_t count, off_t offset) {
    wait_before_read(fd);
    ssize_t result = original_pread(fd, buffer, count, offset);
    count_bytes(fd, result);
    return result;
}

ssize_t pread64(int fd, void *buffer, size_t count, off64_t offset) {
    wait_before_read(fd);
    ssize_t result = original_pread64(fd, buffer, count, offset);
    count_bytes(fd, result);
    return result;
}

__attribute__((destructor)) static void report(void) {
    const char *path = getenv("GWFLOW_LOG_READ_REPORT");
    unsigned long long bytes = atomic_load(&total);
    if (!path || !bytes) return;
    int fd = open(path, O_WRONLY | O_CREAT | O_APPEND, 0600);
    if (fd < 0) return;
    char line[128];
    int length = snprintf(line, sizeof line, "%ld %llu\n", (long)getpid(), bytes);
    ssize_t written = write(fd, line, (size_t)length);
    (void)written;
    close(fd);
}
