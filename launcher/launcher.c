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
 * Because this process is the one holding Full Disk Access, it keeps its job narrow:
 * validate the program it was asked to run, scrub dynamic-linker overrides out of the
 * environment, start the child with clean signal state, forward termination signals and
 * report the child's exit status. It never reads a file and never writes one.
 *
 * Usage: DiskHealth <absolute path to python> -m diskhealth serve
 * Build: cc -O2 -Wall -Wextra -o DiskHealth launcher.c   (done by install.sh)
 */
#include <errno.h>
#include <signal.h>
#include <spawn.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <sys/wait.h>
#include <unistd.h>

extern char **environ;

/* Test hook, compiled out of every real build (the default makes the branch dead code the
 * compiler drops). tests/test_launcher.py sets it to widen the window between posix_spawn()
 * returning and child_pid being set, so the signal race below can actually be reproduced. */
#ifndef DH_SPAWN_RACE_DELAY_US
#define DH_SPAWN_RACE_DELAY_US 0
#endif

/* Written by main, read by the signal handler: sig_atomic_t is the only type the C
 * standard guarantees can be touched from a handler without tearing. */
static volatile sig_atomic_t child_pid = 0;

/* Signals we pass on to the child instead of dying on. */
static const int FORWARDED[] = {SIGTERM, SIGINT, SIGHUP};
#define N_FORWARDED ((int)(sizeof FORWARDED / sizeof FORWARDED[0]))

static void forward(int sig) {
    int saved = errno;                   /* kill() may clobber errno in the interrupted code */
    pid_t p = (pid_t)child_pid;
    if (p > 0) kill(p, sig);
    errno = saved;
}

/* Remove dynamic-linker overrides before starting the child. This process holds Full Disk
 * Access, so Python and everything Python runs (du, mdls, brew, git, docker) inherit that
 * privilege: a DYLD_INSERT_LIBRARIES left in the agent's environment must not become a way
 * to load code into any of them.
 *
 * Scope, stated plainly: this protects the CHILD, not this process. dyld reads DYLD_* before
 * main() runs, and it only ignores them for a binary signed with a Developer ID. This launcher
 * is ad-hoc signed (see README), so it cannot defend its own address space - measured, not
 * assumed: `codesign -o runtime,library` makes no difference to an ad-hoc signature. Setting
 * the agent's environment already requires write access to the LaunchAgent plist in the user's
 * home, so this is defence in depth, not a boundary. */
static void scrub_environment(void) {
    static const char *const prefixes[] = {"DYLD_", "LD_PRELOAD"};
    for (int again = 1; again;) {
        again = 0;
        for (char **e = environ; *e; e++) {
            for (size_t i = 0; i < sizeof prefixes / sizeof prefixes[0]; i++) {
                if (strncmp(*e, prefixes[i], strlen(prefixes[i])) != 0) continue;
                const char *eq = strchr(*e, '=');
                if (!eq) continue;
                char name[256];
                size_t n = (size_t)(eq - *e);
                if (n >= sizeof name) continue;
                memcpy(name, *e, n);
                name[n] = '\0';
                unsetenv(name);          /* invalidates environ: restart the walk */
                again = 1;
                break;
            }
            if (again) break;
        }
    }
}

/* The program must be an absolute path to a regular, executable file. launchd always
 * passes one; refusing anything else turns a misconfigured plist into a clear message
 * instead of an exec of whatever happens to match in the current directory. */
static int check_program(const char *path) {
    struct stat st;
    if (path[0] != '/') {
        fprintf(stderr, "DiskHealth: %s is not an absolute path\n", path);
        return 0;
    }
    if (stat(path, &st) != 0) {
        fprintf(stderr, "DiskHealth: cannot stat %s: %s\n", path, strerror(errno));
        return 0;
    }
    if (!S_ISREG(st.st_mode)) {
        fprintf(stderr, "DiskHealth: %s is not a regular file\n", path);
        return 0;
    }
    if (access(path, X_OK) != 0) {
        fprintf(stderr, "DiskHealth: %s is not executable: %s\n", path, strerror(errno));
        return 0;
    }
    return 1;
}

int main(int argc, char *argv[]) {
    if (argc < 2) {
        fprintf(stderr, "usage: %s <program> [args...]\n", argv[0]);
        return 64;                                   /* EX_USAGE */
    }
    if (!check_program(argv[1])) return 78;          /* EX_CONFIG */

    scrub_environment();

    struct sigaction sa;
    memset(&sa, 0, sizeof sa);
    sa.sa_handler = forward;
    sigemptyset(&sa.sa_mask);
    sa.sa_flags = SA_RESTART;
    for (int i = 0; i < N_FORWARDED; i++) sigaction(FORWARDED[i], &sa, NULL);

    /* Block the forwarded signals across the spawn. Without this, a SIGTERM arriving
     * between posix_spawn() returning and child_pid being set is delivered to a handler
     * that still sees child_pid == 0: the launcher would exit and leave the service
     * running, unsupervised, with nothing left to stop it. */
    sigset_t block, previous;
    sigemptyset(&block);
    for (int i = 0; i < N_FORWARDED; i++) sigaddset(&block, FORWARDED[i]);
    sigprocmask(SIG_BLOCK, &block, &previous);

    /* The child must not inherit that mask, nor any disposition we or launchd set:
     * exec resets handlers, but an *ignored* signal stays ignored across exec. */
    posix_spawnattr_t attr;
    sigset_t empty, all;
    sigemptyset(&empty);
    sigfillset(&all);
    posix_spawnattr_init(&attr);
    posix_spawnattr_setsigmask(&attr, &empty);
    posix_spawnattr_setsigdefault(&attr, &all);
    posix_spawnattr_setflags(&attr, POSIX_SPAWN_SETSIGMASK | POSIX_SPAWN_SETSIGDEF);

    pid_t pid;
    int rc = posix_spawn(&pid, argv[1], NULL, &attr, &argv[1], environ);
    posix_spawnattr_destroy(&attr);
    if (rc != 0) {
        sigprocmask(SIG_SETMASK, &previous, NULL);
        fprintf(stderr, "DiskHealth: cannot start %s: %s\n", argv[1], strerror(rc));
        return 78;                                   /* EX_CONFIG */
    }
    if (DH_SPAWN_RACE_DELAY_US) usleep(DH_SPAWN_RACE_DELAY_US);   /* tests only; 0 in real builds */
    child_pid = (sig_atomic_t)pid;
    sigprocmask(SIG_SETMASK, &previous, NULL);       /* any signal held here fires now, with a pid */

    int status = 0;
    while (waitpid(pid, &status, 0) < 0) {
        if (errno != EINTR) {
            perror("DiskHealth: waitpid");
            return 70;                               /* EX_SOFTWARE */
        }
    }
    child_pid = 0;
    if (WIFEXITED(status)) return WEXITSTATUS(status);
    if (WIFSIGNALED(status)) return 128 + WTERMSIG(status);
    return 1;
}
