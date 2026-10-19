/*
 * NapGuard Stage 02 test workload: an unmodified, checkpoint-unaware process
 * whose state lives only in memory.
 *
 * State (see experiments/stage02/ngexp/model.py for the exact definition):
 *   - a 128-bit random nonce chosen at startup (fresh start => new nonce);
 *   - per role (main, worker threads t0.., forked child) a counter that
 *     advances every tick and a digest chain over the counter;
 *   - an optional ballast buffer filled from the nonce, never modified,
 *     whose digest is recomputed from memory at every report.
 *
 * Observation: every --report-every ticks each role appends one NGSTATE line
 * to --report PATH using open(O_APPEND)/write/close. No descriptor for the
 * report file stays open between reports. The program NEVER reads that file,
 * or any other saved state; it has no persistence or recovery code at all.
 *
 * Optional open-file classes:
 *   --hold-file PATH        keep PATH open for writing; write one header only
 *   --hold-file-grow        also append to the held file at every report
 *
 * Build: cc -O2 -Wall -Wextra -pthread -o ngcounter ngcounter.c
 */
#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <inttypes.h>
#include <pthread.h>
#include <signal.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>
#ifdef __APPLE__
#include <sys/random.h>
#endif

#define SALT_MAIN 0x0ULL
#define SALT_THREAD_BASE 0x100ULL
#define SALT_CHILD 0x200ULL
#define MAX_THREADS 16

static uint64_t nonce_hi, nonce_lo;
static const char *report_path;
static const char *run_label = "unlabeled";
static long tick_ms = 10;
static long report_every = 20;
static uint64_t *ballast;
static size_t ballast_words;
static int hold_fd = -1;
static int hold_grow;
static volatile sig_atomic_t stop_requested;

static uint64_t splitmix64(uint64_t x) {
  uint64_t z = x + 0x9E3779B97F4A7C15ULL;
  z = (z ^ (z >> 30)) * 0xBF58476D1CE4E5B9ULL;
  z = (z ^ (z >> 27)) * 0x94D049BB133111EBULL;
  return z ^ (z >> 31);
}

/* The kernel PID, bypassing libc (a checkpoint library may virtualize getpid). */
static long kernel_pid(void) {
#if defined(__linux__) && defined(__aarch64__)
  register long x8 __asm__("x8") = 172; /* __NR_getpid on arm64 */
  register long x0 __asm__("x0");
  __asm__ volatile("svc #0" : "=r"(x0) : "r"(x8) : "memory");
  return x0;
#elif defined(__linux__) && defined(__x86_64__)
  long ret;
  __asm__ volatile("syscall" : "=a"(ret) : "a"(39L) : "rcx", "r11", "memory");
  return ret;
#else
  return -1;
#endif
}

static uint64_t mono_ns(void) {
  struct timespec ts;
  clock_gettime(CLOCK_MONOTONIC, &ts);
  return (uint64_t)ts.tv_sec * 1000000000ULL + (uint64_t)ts.tv_nsec;
}

static uint64_t ballast_digest(void) {
  uint64_t h = splitmix64(nonce_hi);
  for (size_t i = 0; i < ballast_words; i++) h = splitmix64(h ^ ballast[i]);
  return h;
}

typedef struct {
  char role[8];
  uint64_t counter;
  uint64_t digest;
} chain_t;

static void chain_init(chain_t *c, const char *role, uint64_t salt) {
  snprintf(c->role, sizeof c->role, "%s", role);
  c->counter = 0;
  c->digest = splitmix64(nonce_hi ^ nonce_lo ^ salt);
}

static void chain_step(chain_t *c) {
  c->counter++;
  c->digest = splitmix64(c->digest ^ c->counter);
}

static void report(const chain_t *c) {
  char line[512];
  char ballast_hex[24] = "-";
  char kpid_txt[24] = "-";
  long kp = kernel_pid();
  if (ballast) snprintf(ballast_hex, sizeof ballast_hex, "%016" PRIx64, ballast_digest());
  if (kp > 0) snprintf(kpid_txt, sizeof kpid_txt, "%ld", kp);
  int n = snprintf(line, sizeof line,
                   "NGSTATE v1 run=%s role=%s pid=%ld kpid=%s nonce=%016" PRIx64 "%016" PRIx64
                   " counter=%" PRIu64 " digest=%016" PRIx64 " ballast=%s mono_ns=%" PRIu64 "\n",
                   run_label, c->role, (long)getpid(), kpid_txt, nonce_hi, nonce_lo, c->counter,
                   c->digest, ballast_hex, mono_ns());
  if (n <= 0 || (size_t)n >= sizeof line) return;
  int fd = open(report_path, O_WRONLY | O_APPEND | O_CREAT | O_CLOEXEC, 0600);
  if (fd < 0) return;
  ssize_t ignored = write(fd, line, (size_t)n);
  (void)ignored;
  close(fd);
  if (hold_fd >= 0 && hold_grow) {
    ignored = write(hold_fd, line, (size_t)n);
    (void)ignored;
  }
}

static void sleep_tick(void) {
  struct timespec ts = {tick_ms / 1000, (tick_ms % 1000) * 1000000L};
  while (nanosleep(&ts, &ts) == -1 && errno == EINTR && !stop_requested) {
  }
}

static void run_chain(const char *role, uint64_t salt) {
  chain_t c;
  chain_init(&c, role, salt);
  report(&c);
  while (!stop_requested) {
    sleep_tick();
    chain_step(&c);
    if (c.counter % (uint64_t)report_every == 0) report(&c);
  }
}

static void *thread_main(void *arg) {
  long idx = (long)(intptr_t)arg;
  char role[8];
  snprintf(role, sizeof role, "t%ld", idx);
  run_chain(role, SALT_THREAD_BASE + (uint64_t)idx);
  return NULL;
}

static void on_term(int sig) {
  (void)sig;
  stop_requested = 1;
}

static void usage(void) {
  fprintf(stderr,
          "usage: ngcounter --report PATH [--label L] [--tick-ms N] [--report-every N]\n"
          "                 [--threads N] [--fork-child] [--ballast-mib N]\n"
          "                 [--hold-file PATH [--hold-file-grow]]\n");
  exit(2);
}

static long parse_long(const char *s, long lo, long hi) {
  char *end = NULL;
  errno = 0;
  long v = strtol(s, &end, 10);
  if (errno || !end || *end || v < lo || v > hi) usage();
  return v;
}

int main(int argc, char **argv) {
  long threads = 0, ballast_mib = 0;
  int fork_child = 0;
  const char *hold_path = NULL;
  for (int i = 1; i < argc; i++) {
    const char *a = argv[i];
    if (!strcmp(a, "--report") && i + 1 < argc) report_path = argv[++i];
    else if (!strcmp(a, "--label") && i + 1 < argc) run_label = argv[++i];
    else if (!strcmp(a, "--tick-ms") && i + 1 < argc) tick_ms = parse_long(argv[++i], 1, 1000);
    else if (!strcmp(a, "--report-every") && i + 1 < argc)
      report_every = parse_long(argv[++i], 1, 100000);
    else if (!strcmp(a, "--threads") && i + 1 < argc)
      threads = parse_long(argv[++i], 0, MAX_THREADS);
    else if (!strcmp(a, "--fork-child")) fork_child = 1;
    else if (!strcmp(a, "--ballast-mib") && i + 1 < argc)
      ballast_mib = parse_long(argv[++i], 0, 1024);
    else if (!strcmp(a, "--hold-file") && i + 1 < argc) hold_path = argv[++i];
    else if (!strcmp(a, "--hold-file-grow")) hold_grow = 1;
    else usage();
  }
  if (!report_path || strpbrk(run_label, " \t\n=")) usage();

  uint64_t seed[2];
  if (getentropy(seed, sizeof seed) != 0) {
    perror("getentropy");
    return 1;
  }
  nonce_hi = seed[0];
  nonce_lo = seed[1];

  if (ballast_mib > 0) {
    ballast_words = (size_t)ballast_mib * 1024 * 1024 / 8;
    ballast = malloc(ballast_words * sizeof *ballast);
    if (!ballast) {
      perror("malloc");
      return 1;
    }
    for (size_t i = 0; i < ballast_words; i++) ballast[i] = splitmix64(nonce_lo + i);
  }

  if (hold_path) {
    hold_fd = open(hold_path, O_WRONLY | O_CREAT | O_TRUNC | O_CLOEXEC, 0600);
    if (hold_fd < 0) {
      perror("open --hold-file");
      return 1;
    }
    char header[96];
    int n = snprintf(header, sizeof header, "NGHOLD v1 nonce=%016" PRIx64 "%016" PRIx64 "\n",
                     nonce_hi, nonce_lo);
    if (n > 0 && write(hold_fd, header, (size_t)n) != n) return 1;
  }

  struct sigaction sa;
  memset(&sa, 0, sizeof sa);
  sa.sa_handler = on_term;
  sigaction(SIGTERM, &sa, NULL);
  sigaction(SIGINT, &sa, NULL);

  pid_t child = -1;
  if (fork_child) {
    child = fork();
    if (child < 0) {
      perror("fork");
      return 1;
    }
    if (child == 0) {
      run_chain("child", SALT_CHILD);
      _exit(0);
    }
  }

  pthread_t tids[MAX_THREADS];
  for (long t = 0; t < threads; t++)
    if (pthread_create(&tids[t], NULL, thread_main, (void *)(intptr_t)t) != 0) return 1;

  run_chain("main", SALT_MAIN);

  for (long t = 0; t < threads; t++) pthread_join(tids[t], NULL);
  if (child > 0) {
    kill(child, SIGTERM); /* the child has its own copy of stop_requested */
    while (waitpid(child, NULL, 0) < 0 && errno == EINTR) {
    }
  }
  return 0;
}
