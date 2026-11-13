/*
 * DiskHealth launcher.
 *
 * Full Disk Access is granted per program. Granting it to Homebrew's Python is awkward
 * (the real binary lives inside Python.framework and its path changes on every
 * `brew upgrade python`). Instead the LaunchAgent starts this tiny, stable program,
 * which starts Python as a child process. macOS attributes the child's file access to
 * this "responsible" program, so one Full Disk Access grant for DiskHealth.app covers
 * Python, du, mdls ... and survives Python upgrades.
 *
 * Usage: DiskHealth <python> -m diskhealth serve
 * Build: cc -O2 -o DiskHealth launcher.c   (done by install.sh)
 */
#include <errno.h>
#include <signal.h>
#include <spawn.h>
#include <stdio.h>
#include <string.h>
#include <sys/wait.h>
#include <unistd.h>

extern char **environ;
static volatile pid_t child = 0;

static void forward(int sig) {
    if (child > 0) kill(child, sig);
}

int main(int argc, char *argv[]) {
    if (argc < 2) {
        fprintf(stderr, "usage: %s <program> [args...]\n", argv[0]);
        return 64;
    }
    struct sigaction sa;
    memset(&sa, 0, sizeof sa);
    sa.sa_handler = forward;
    sigemptyset(&sa.sa_mask);
    sigaction(SIGTERM, &sa, NULL);
    sigaction(SIGINT, &sa, NULL);
    sigaction(SIGHUP, &sa, NULL);

    pid_t pid;
    int rc = posix_spawn(&pid, argv[1], NULL, NULL, &argv[1], environ);
    if (rc != 0) {
        fprintf(stderr, "DiskHealth: cannot start %s: %s\n", argv[1], strerror(rc));
        return 78;
    }
    child = pid;

    int status = 0;
    while (waitpid(pid, &status, 0) < 0) {
        if (errno != EINTR) {
            perror("DiskHealth: waitpid");
            return 70;
        }
    }
    if (WIFEXITED(status)) return WEXITSTATUS(status);
    if (WIFSIGNALED(status)) return 128 + WTERMSIG(status);
    return 1;
}
