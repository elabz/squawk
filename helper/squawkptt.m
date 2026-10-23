/* SquawkPTT — resident push-to-talk helper for squawk.
 *
 * This helper owns the whole push-to-talk trigger. A CGEventTap watches the
 * keyboard and, only while iTerm is frontmost, tells a *tap* of Space apart
 * from a *hold*: a tap types a normal space, a hold past SQUAWK_HOLD_MS starts
 * recording and release stops it. It shells to `~/bin/squawk ptt-start` /
 * `ptt-stop` in its own process so the microphone TCC grant applies.
 *
 * Why a signed app bundle and not a bare tool: TCC grants attach to the
 * *responsible process*, and macOS only renders a mic consent prompt for a
 * bundled, signed app asking on its own behalf. A bare child (sox) asking just
 * hangs forever in "not determined" — verified empirically. This bundle holds
 * the grant; children inherit it through the responsible-process chain. The
 * same identity is what lets it hold the Accessibility grant the event tap
 * needs. (This replaces the former Karabiner rule + trigger-file poll loop —
 * no external key-remapper is involved anymore.)
 *
 * Permissions: Accessibility (to create a listen-and-alter tap and to post the
 * synthetic space for a tap) and Input Monitoring (to observe keys), plus
 * Microphone. Without Accessibility the tap simply never comes up and the
 * keyboard behaves normally; the helper logs guidance and keeps retrying so a
 * later grant takes effect without a restart.
 */

#import <AVFoundation/AVFoundation.h>
#import <AppKit/AppKit.h>
#import <ApplicationServices/ApplicationServices.h>

#include <errno.h>
#include <limits.h>
#include <signal.h>
#include <spawn.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/wait.h>
#include <unistd.h>

extern char **environ;

/* Virtual keycode for Space (Carbon kVK_Space). */
#define KEYCODE_SPACE 49

/* Stamped onto the synthetic space we post for a tap, so the tap recognizes
 * and passes through its own events instead of recursing on them. */
#define SQUAWK_EVENT_MARKER 0x5157414B /* "QWAK" */

/* Default hold threshold in ms — matches the tuned Karabiner value. */
#define DEFAULT_HOLD_MS 400.0

/* Recording cap. The helper enforces it because it alone knows when the hold
 * began: CAP_WARN_SECONDS before the cap it plays the warning cue and runs
 * `squawk ptt-warn` (badge countdown); at the cap it finishes the dictation
 * exactly as a release would, so a long recording is transcribed, never lost.
 * Defaults and the warning lead must match DEFAULT_MAX_SECONDS and
 * CAP_WARN_SECONDS in squawk.py. */
#define DEFAULT_MAX_SECONDS 300.0
#define CAP_WARN_SECONDS 15.0

static char squawk_path[PATH_MAX];

/* Locate the squawk CLI. `install-agent` writes the exact path of the CLI that
 * ran it into the LaunchAgent's SQUAWK_CLI, which is authoritative — where the
 * CLI lives depends entirely on how squawk was installed (Homebrew keg, ~/bin
 * via the curl installer, or a repo checkout), and a launchd agent inherits no
 * useful PATH to search. The fallbacks cover an agent installed before
 * SQUAWK_CLI existed, or a hand-written plist. Returns NO if nothing usable was
 * found, so startup can say so instead of failing silently on the first hold. */
static BOOL resolve_squawk_path(const char *home) {
    const char *explicit_path = getenv("SQUAWK_CLI");
    if (explicit_path && *explicit_path && access(explicit_path, X_OK) == 0) {
        snprintf(squawk_path, sizeof squawk_path, "%s", explicit_path);
        return YES;
    }
    if (explicit_path && *explicit_path)
        fprintf(stderr, "squawkptt: SQUAWK_CLI=%s is not executable — falling back\n",
                explicit_path);

    const char *fallbacks[] = {"/opt/homebrew/bin/squawk", "/usr/local/bin/squawk"};
    char candidate[PATH_MAX];
    snprintf(candidate, sizeof candidate, "%s/bin/squawk", home);
    if (access(candidate, X_OK) == 0) {
        snprintf(squawk_path, sizeof squawk_path, "%s", candidate);
        return YES;
    }
    for (size_t i = 0; i < sizeof fallbacks / sizeof *fallbacks; i++) {
        if (access(fallbacks[i], X_OK) == 0) {
            snprintf(squawk_path, sizeof squawk_path, "%s", fallbacks[i]);
            return YES;
        }
    }
    return NO;
}

static double g_hold_ms = DEFAULT_HOLD_MS;
static double g_max_seconds = DEFAULT_MAX_SECONDS;
static CFMachPortRef g_tap = NULL;

/* Trigger state machine (all touched only on the main run loop):
 *   g_armed     — Space is down, threshold timer pending, not yet recording
 *   g_recording — threshold crossed, ptt-start issued, waiting for release
 *   g_hold_gen  — bumped on every state transition; a pending threshold timer
 *                 only fires if the generation it captured is still current,
 *                 which is how a release (tap) cancels the arming timer. */
static BOOL g_armed = NO;
static BOOL g_recording = NO;
static uint64_t g_hold_gen = 0;
/* Set when this Space press has already been resolved without recording —
 * emitted early as a typed space (another key went down while it was armed),
 * or finished by the recording cap. The press's remaining auto-repeats and its
 * key-up are swallowed so they can't type a second space. */
static BOOL g_consumed = NO;

/* Set when the legacy Karabiner Space rule is still present: the tap refuses to
 * arm so Space is never grabbed by two owners at once. Re-checked periodically
 * so running `squawk migrate` clears it without restarting the helper. */
static BOOL g_conflict = NO;

/* Serial queue for squawk subcommand runs. Serialization is load-bearing: a
 * quick release right after the hold threshold used to spawn ptt-stop while
 * ptt-start (a cold Python interpreter) was still setting up its lock file —
 * the stop no-opped, the recorder it was meant to kill started *afterwards*,
 * and the session sat stuck in listening mode until the recording cap. Running
 * every subcommand to completion, in order, on one queue makes that race
 * impossible. Created in main before the tap can fire. */
static dispatch_queue_t g_squawk_queue;

/* Run `squawk <subcommand>` off the event-tap thread and return immediately —
 * never block the tap callback, a slow callback trips kCGEventTapDisabledBy-
 * Timeout. Each subcommand is waited on (squawk itself detaches sox and the
 * transcription worker, so these exit in well under a second) purely so the
 * next one cannot overtake it. */
static void run_squawk_arg(const char *subcommand, const char *arg);
static void run_squawk(const char *subcommand) { run_squawk_arg(subcommand, NULL); }

static void run_squawk_arg(const char *subcommand, const char *arg) {
    if (!squawk_path[0]) {
        fprintf(stderr, "squawkptt: cannot run `squawk %s` — no CLI was found at "
                        "startup. Run `squawk install-agent` to record its path.\n",
                subcommand);
        return;
    }
    char *arg_copy = arg ? strdup(arg) : NULL; /* outlives the caller's buffer */
    dispatch_async(g_squawk_queue, ^{
        char *argv[] = {squawk_path, (char *)subcommand, arg_copy, NULL};
        pid_t pid;
        if (posix_spawn(&pid, squawk_path, NULL, NULL, argv, environ) != 0) {
            perror("squawkptt: posix_spawn squawk");
            free(arg_copy);
            return;
        }
        int status;
        while (waitpid(pid, &status, 0) < 0 && errno == EINTR)
            ;
        free(arg_copy);
    });
}

/* The start and release cues, preloaded at launch and played by the helper
 * itself, the instant the hold threshold is crossed or Space comes up. They
 * used to be played by the squawk CLI, so every cue waited on a cold Python
 * interpreter plus an afplay exec — two process launches that, on a loaded
 * machine, turned an instant cue into a multi-second one. A resident process
 * playing an already-decoded sound has no launch on the path at all. The CLI
 * is told via SQUAWK_HELPER_CUES (set in main, inherited by every spawn) so it
 * does not play them a second time. */
static NSSound *g_cue_start;
static NSSound *g_cue_release;
static NSSound *g_cue_warn; /* CAP_WARN_SECONDS before the recording cap */

/* Even a warm -play costs 10-50 ms on a loaded machine, which must not be spent
 * inside the tap callback (the whole keyboard waits on it). Queued to the main
 * queue instead, it runs the moment the callback returns. */
static void play_cue(NSSound *cue) {
    dispatch_async(dispatch_get_main_queue(), ^{
        /* stop first: a cue still sounding from a fast tap-hold-tap would
         * otherwise make -play a no-op. */
        [cue stop];
        [cue play];
    });
}

/* The first -play in a process sets up the audio output (~200-300 ms,
 * measured). Pay that once at launch with a muted play, not on the first hold. */
static void warm_cue(NSSound *cue) {
    cue.volume = 0;
    [cue play];
    dispatch_after(dispatch_time(DISPATCH_TIME_NOW, 600 * NSEC_PER_MSEC),
                   dispatch_get_main_queue(), ^{
        [cue stop];
        cue.volume = 1;
    });
}

/* Post a genuine Space keypress (down+up) for a tap we swallowed, marked so the
 * tap ignores it. Posted through the tap proxy, NOT CGEventPost: proxy posting
 * inserts the pair at this tap's own position in the event stream, so it stays
 * ordered before any key event the tap has not processed yet. CGEventPost
 * re-enters the pipeline from the top asynchronously, and a letter typed right
 * after the tap could overtake the space — the "space arrives after the next
 * letter" bug. */
static void synthesize_space(CGEventTapProxy proxy, CGEventRef then) {
    CGEventSourceRef src = CGEventSourceCreate(kCGEventSourceStateHIDSystemState);
    CGEventRef down = CGEventCreateKeyboardEvent(src, (CGKeyCode)KEYCODE_SPACE, true);
    CGEventRef up = CGEventCreateKeyboardEvent(src, (CGKeyCode)KEYCODE_SPACE, false);
    CGEventSetIntegerValueField(down, kCGEventSourceUserData, SQUAWK_EVENT_MARKER);
    CGEventSetIntegerValueField(up, kCGEventSourceUserData, SQUAWK_EVENT_MARKER);
    CGEventTapPostEvent(proxy, down);
    CGEventTapPostEvent(proxy, up);
    /* `then` is a key the user pressed while Space was still undecided. It is
     * re-posted (as a marked copy) through the same proxy right behind the
     * space, rather than returned from the callback, so the two can only ever
     * arrive in the order they were typed — no reliance on how posted events
     * interleave with a returned one. */
    if (then) {
        CGEventRef copy = CGEventCreateCopy(then);
        if (copy) {
            CGEventSetIntegerValueField(copy, kCGEventSourceUserData, SQUAWK_EVENT_MARKER);
            CGEventTapPostEvent(proxy, copy);
            CFRelease(copy);
        }
    }
    if (down) CFRelease(down);
    if (up) CFRelease(up);
    if (src) CFRelease(src);
}

/* Whether iTerm is the frontmost app, maintained from NSWorkspace activation
 * notifications on the main thread (same thread as the tap callback, so no
 * locking). The tap callback used to ask NSWorkspace directly on every Space
 * event; that is an XPC round-trip, and a slow answer stalls the whole
 * keyboard and can trip kCGEventTapDisabledByTimeout — the source of the
 * multi-second "nothing happens" delays. */
static BOOL g_iterm_front = NO;

static BOOL app_is_iterm(NSRunningApplication *app) {
    return [app.bundleIdentifier isEqualToString:@"com.googlecode.iterm2"];
}

/* Abandon any in-flight hold, issuing ptt-stop if recording actually started.
 * Called whenever the helper can no longer trust that it will see the Space
 * release (focus left iTerm, the tap was disabled, a conflict appeared).
 * Without the ptt-stop, sox kept recording and the session stayed stuck in
 * listening mode (red cursor + mic icon) with releases doing nothing. */
static void abandon_hold(const char *why) {
    if (g_recording) {
        fprintf(stderr, "squawkptt: stopping in-flight recording (%s)\n", why);
        play_cue(g_cue_release);
        run_squawk("ptt-stop");
    }
    g_armed = NO;
    g_recording = NO;
    g_consumed = NO;
    g_hold_gen++;
}

/* ── Self-update ──────────────────────────────────────────────────────────
 * `brew upgrade squawk` replaces the prebuilt, Developer-ID-signed helper in
 * the keg (…/libexec/squawk/SquawkPTT.app) but cannot touch the running copy
 * in ~/Applications: Homebrew's sandbox forbids writing to $HOME or loading a
 * launchd agent. So the helper watches the keg and, once a different bundle
 * version appears and no dictation is in flight, runs `squawk install-agent`
 * itself — which copies the new app over this one and restarts the agent.
 * Because every release carries the same Developer ID signature, the TCC
 * grants carry over and the upgrade needs nothing from the user.
 *
 * Only a *prebuilt* app in the keg counts: a source checkout or the curl
 * installer would rebuild ad-hoc, which re-prompts for every grant — that must
 * stay a deliberate `squawk install-agent`, never something done behind the
 * user's back. */

/* The keg's prebuilt helper, derived from the CLI's real path
 * (<keg>/bin/squawk → <keg>/libexec/squawk/SquawkPTT.app). Re-derived on each
 * check, because each upgrade moves the CLI to a new versioned keg. */
static NSString *keg_helper_version(void) {
    char real[PATH_MAX];
    if (!squawk_path[0] || !realpath(squawk_path, real))
        return nil;
    NSString *keg = [[[NSString stringWithUTF8String:real]
        stringByDeletingLastPathComponent] stringByDeletingLastPathComponent];
    NSString *plist = [keg stringByAppendingPathComponent:
        @"libexec/squawk/SquawkPTT.app/Contents/Info.plist"];
    NSDictionary *info = [NSDictionary dictionaryWithContentsOfFile:plist];
    id v = info[@"CFBundleVersion"];
    return [v isKindOfClass:[NSString class]] ? v : nil;
}

static NSString *g_update_attempted; /* tried once per version, never in a loop */

static void maybe_self_update(void) {
    if (g_armed || g_recording || g_consumed)
        return; /* never restart under a dictation */
    NSString *mine = [[NSBundle mainBundle] objectForInfoDictionaryKey:@"CFBundleVersion"];
    NSString *keg = keg_helper_version();
    if (!keg || !mine || [keg isEqualToString:mine] || [keg isEqualToString:g_update_attempted])
        return;
    g_update_attempted = keg;
    fprintf(stderr, "squawkptt: Homebrew installed helper %s (running %s) — updating\n",
            keg.UTF8String, mine.UTF8String);
    /* Its own session: install-agent boots this agent out, and launchd kills
     * the job's process group with it — verified; a setsid child survives. */
    posix_spawnattr_t attr;
    posix_spawnattr_init(&attr);
    posix_spawnattr_setflags(&attr, POSIX_SPAWN_SETSID);
    char *argv[] = {squawk_path, "install-agent", NULL};
    pid_t pid;
    if (posix_spawn(&pid, squawk_path, NULL, &attr, argv, environ) != 0)
        perror("squawkptt: posix_spawn install-agent");
    posix_spawnattr_destroy(&attr);
    /* Not waited on: it outlives us by design. Reaped by launchd afterwards. */
}

/* True if karabiner.json still carries the legacy squawk/vox Space rule. The
 * rule's shell_command touches ~/.local/state/squawk/ptt/… (or the older vox
 * path), so those literal path fragments are a reliable marker in the file. */
static BOOL legacy_rule_active(void) {
    const char *home = getenv("HOME");
    if (!home || !*home)
        return NO;
    char path[PATH_MAX];
    snprintf(path, sizeof path, "%s/.config/karabiner/karabiner.json", home);
    FILE *f = fopen(path, "rb");
    if (!f)
        return NO;
    BOOL found = NO;
    char buf[8192];
    size_t carry = 0;
    /* Read in chunks, keeping a small overlap so a marker split across two
     * reads is still matched. */
    for (;;) {
        size_t n = fread(buf + carry, 1, sizeof buf - carry - 1, f);
        if (n == 0 && carry == 0)
            break;
        buf[carry + n] = '\0';
        if (strstr(buf, "state/squawk/ptt") || strstr(buf, "state/vox/ptt") ||
            strstr(buf, "hold_space_dictate")) {
            found = YES;
            break;
        }
        if (n == 0)
            break;
        size_t keep = 32;
        if (carry + n > keep) {
            memmove(buf, buf + carry + n - keep, keep);
            carry = keep;
        } else {
            carry += n;
        }
    }
    fclose(f);
    return found;
}

/* Arm the cap timers for the recording that just started under hold
 * generation `gen`. Both are cancelled by any state change (release, abandon),
 * because they only act while that generation is still current. */
static double read_config_double(const char *key, double fallback);

static void schedule_cap(uint64_t gen) {
    /* Re-read per recording (a small file read, off the tap callback) so a
     * changed SQUAWK_MAX_SECONDS applies without restarting the helper. */
    double cap = read_config_double("SQUAWK_MAX_SECONDS", g_max_seconds);
    double started = [[NSDate date] timeIntervalSince1970];
    if (cap > CAP_WARN_SECONDS * 2) {
        dispatch_after(dispatch_time(DISPATCH_TIME_NOW,
                                     (int64_t)((cap - CAP_WARN_SECONDS) * NSEC_PER_SEC)),
                       dispatch_get_main_queue(), ^{
            if (!g_recording || gen != g_hold_gen)
                return;
            play_cue(g_cue_warn);
            /* The deadline is absolute so the countdown cannot drift by however
             * long the CLI takes to start. */
            char deadline[32];
            snprintf(deadline, sizeof deadline, "%.3f", started + cap);
            run_squawk_arg("ptt-warn", deadline);
        });
    }
    dispatch_after(dispatch_time(DISPATCH_TIME_NOW, (int64_t)(cap * NSEC_PER_SEC)),
                   dispatch_get_main_queue(), ^{
        if (!g_recording || gen != g_hold_gen)
            return;
        /* Finish exactly like a release, then ignore the real release. */
        fprintf(stderr, "squawkptt: recording reached the %.0f s cap — finishing it\n", cap);
        g_recording = NO;
        g_consumed = YES;
        g_hold_gen++;
        play_cue(g_cue_release);
        run_squawk("ptt-stop");
    });
}

static CGEventRef tap_callback(CGEventTapProxy proxy, CGEventType type,
                               CGEventRef event, void *refcon) {
    (void)refcon;

    /* macOS disabled the tap (our callback was too slow, or user input raced):
     * re-enable it so hold-to-talk keeps working without a restart. While the
     * tap was down every event passed through unseen — including, possibly, the
     * Space release of a hold in flight — so any such hold must be abandoned
     * now or the recording runs on with no way to stop it. */
    if (type == kCGEventTapDisabledByTimeout ||
        type == kCGEventTapDisabledByUserInput) {
        if (g_tap)
            CGEventTapEnable(g_tap, true);
        abandon_hold("event tap was disabled");
        return event;
    }

    if (type != kCGEventKeyDown && type != kCGEventKeyUp)
        return event;

    /* Ignore the events we post ourselves, so we never recurse. */
    if (CGEventGetIntegerValueField(event, kCGEventSourceUserData) == SQUAWK_EVENT_MARKER)
        return event;

    int64_t keycode = CGEventGetIntegerValueField(event, kCGKeyboardEventKeycode);
    if (keycode != KEYCODE_SPACE) {
        /* Another key went down while Space was still undecided: that is fast
         * typing with key rollover ("a", Space, "b" with Space released after
         * "b" went down), never a hold. Resolve the press as a typed space NOW,
         * ahead of this key. Deciding only on Space's release let "b" through
         * first and produced "ab " — the misplaced-space bug. */
        if (type == kCGEventKeyDown && g_armed && g_iterm_front && !g_conflict) {
            g_armed = NO;
            g_consumed = YES;
            g_hold_gen++; /* cancels the pending threshold timer */
            synthesize_space(proxy, event);
            return NULL; /* re-posted behind the space by synthesize_space */
        }
        return event; /* every other non-Space key passes through untouched */
    }

    /* Only alter Space in iTerm, and never while the legacy rule could also be
     * grabbing it. Any other context is passed through completely unmodified. */
    if (g_conflict || !g_iterm_front) {
        /* Focus left iTerm mid-press (or a conflict appeared): abandon the
         * hold, stopping the recorder if one was already running. */
        if (g_armed || g_recording)
            abandon_hold("focus left iTerm mid-press");
        /* The swallowed key-up of a resolved press will now go to another app;
         * forget it, or the next Space in iTerm would be swallowed instead. */
        g_consumed = NO;
        return event;
    }

    if (type == kCGEventKeyDown) {
        /* Auto-repeat while armed, recording, or already resolved: swallow so
         * a held Space never spews spaces. */
        if (g_armed || g_recording || g_consumed)
            return NULL;

        g_armed = YES;
        uint64_t gen = ++g_hold_gen;
        dispatch_after(dispatch_time(DISPATCH_TIME_NOW, (int64_t)(g_hold_ms * NSEC_PER_MSEC)),
                       dispatch_get_main_queue(), ^{
            /* Still the same, still-held press? Then it's a hold: start. */
            if (g_armed && !g_recording && gen == g_hold_gen) {
                g_armed = NO;
                g_recording = YES;
                play_cue(g_cue_start); /* before the spawn: the ack never waits */
                run_squawk("ptt-start");
                schedule_cap(gen);
            }
        });
        return NULL; /* swallow the down; we decide tap-vs-hold on release/timer */
    }

    /* keyUp */
    if (g_consumed) {
        g_consumed = NO; /* already typed early, or finished by the cap */
        return NULL;
    }
    if (g_recording) {
        g_recording = NO;
        g_hold_gen++;
        play_cue(g_cue_release);
        run_squawk("ptt-stop");
        return NULL;
    }
    if (g_armed) {
        /* Released before the threshold → a tap. Cancel the pending timer (via
         * the generation bump) and emit a real space. */
        g_armed = NO;
        g_hold_gen++;
        synthesize_space(proxy, NULL);
        return NULL;
    }
    return event; /* a Space up we never swallowed the down for — leave it */
}

/* Create the tap and wire it to the main run loop. Returns NO if the tap could
 * not be created (Accessibility not yet granted). */
static BOOL install_tap(void) {
    CGEventMask mask = CGEventMaskBit(kCGEventKeyDown) | CGEventMaskBit(kCGEventKeyUp);
    g_tap = CGEventTapCreate(kCGSessionEventTap, kCGHeadInsertEventTap,
                             kCGEventTapOptionDefault, mask, tap_callback, NULL);
    if (!g_tap)
        return NO;
    CFRunLoopSourceRef src = CFMachPortCreateRunLoopSource(kCFAllocatorDefault, g_tap, 0);
    CFRunLoopAddSource(CFRunLoopGetMain(), src, kCFRunLoopCommonModes);
    if (src) CFRelease(src);
    CGEventTapEnable(g_tap, true);
    return YES;
}

/* Read SQUAWK_HOLD_MS from the environment, then fall back to the squawk config
 * file (~/.config/squawk/config, KEY=value). Ignores non-positive / unparsable
 * values, keeping the default. */
static double read_config_double(const char *key, double fallback) {
    const char *env = getenv(key);
    if (env && *env) {
        double v = atof(env);
        if (v > 0)
            return v;
    }
    const char *home = getenv("HOME");
    if (home && *home) {
        char path[PATH_MAX];
        snprintf(path, sizeof path, "%s/.config/squawk/config", home);
        FILE *f = fopen(path, "r");
        if (f) {
            char line[256];
            double found = 0;
            size_t klen = strlen(key);
            while (fgets(line, sizeof line, f)) {
                if (strncmp(line, key, klen) == 0 && line[klen] == '=') {
                    /* tolerate quoting, as squawk.py's unquote() does */
                    const char *v = line + klen + 1;
                    if (*v == '"' || *v == '\'')
                        v++;
                    double d = atof(v);
                    if (d > 0)
                        found = d;
                }
            }
            fclose(f);
            if (found > 0)
                return found;
        }
    }
    return fallback;
}

int main(void) {
    const char *home = getenv("HOME");
    if (!home || !*home) {
        fprintf(stderr, "squawkptt: HOME not set\n");
        return 1;
    }
    if (resolve_squawk_path(home))
        fprintf(stderr, "squawkptt: squawk CLI at %s\n", squawk_path);
    else
        fprintf(stderr, "squawkptt: NO squawk CLI FOUND — checked $SQUAWK_CLI, "
                        "%s/bin/squawk, /opt/homebrew/bin/squawk, "
                        "/usr/local/bin/squawk. Push-to-talk will do nothing. "
                        "Re-run `squawk install-agent` to record the right path.\n",
                home);

    /* Every squawk child is spawned and reaped on this serial queue (see
     * run_squawk) — no SIGCHLD/SIG_IGN needed, and ptt-start / ptt-stop can
     * never overtake each other. */
    g_squawk_queue = dispatch_queue_create("sh.squawk.ptt.spawn", DISPATCH_QUEUE_SERIAL);

    /* Same files the CLI used to play (SOUND_START / SOUND_ACK in squawk.py). */
    g_cue_start = [[NSSound alloc] initWithContentsOfFile:@"/System/Library/Sounds/Pop.aiff"
                                              byReference:YES];
    g_cue_release = [[NSSound alloc] initWithContentsOfFile:@"/System/Library/Sounds/Ping.aiff"
                                                byReference:YES];
    g_cue_warn = [[NSSound alloc] initWithContentsOfFile:@"/System/Library/Sounds/Submarine.aiff"
                                             byReference:YES];
    if (!g_cue_start || !g_cue_release || !g_cue_warn)
        fprintf(stderr, "squawkptt: could not load cue sounds — holds will be silent\n");
    warm_cue(g_cue_start);
    warm_cue(g_cue_release);
    warm_cue(g_cue_warn);
    /* Inherited by every spawned squawk: it skips the cues played above. */
    setenv("SQUAWK_HELPER_CUES", "1", 1);

    /* Track iTerm frontmost-ness from activation notifications instead of
     * querying NSWorkspace inside the tap callback (see g_iterm_front). A
     * deactivation mid-hold also has to abandon the hold here, because Space's
     * release will be delivered to whatever app took focus. */
    NSWorkspace *workspace = [NSWorkspace sharedWorkspace];
    g_iterm_front = app_is_iterm(workspace.frontmostApplication);
    [[workspace notificationCenter]
        addObserverForName:NSWorkspaceDidActivateApplicationNotification
                    object:nil
                     queue:[NSOperationQueue mainQueue]
                usingBlock:^(NSNotification *note) {
        NSRunningApplication *app = note.userInfo[NSWorkspaceApplicationKey];
        BOOL front = app_is_iterm(app);
        if (front == g_iterm_front)
            return;
        g_iterm_front = front;
        if (!front && (g_armed || g_recording))
            abandon_hold("iTerm deactivated mid-press");
    }];

    g_hold_ms = read_config_double("SQUAWK_HOLD_MS", DEFAULT_HOLD_MS);
    g_max_seconds = read_config_double("SQUAWK_MAX_SECONDS", DEFAULT_MAX_SECONDS);
    fprintf(stderr, "squawkptt: hold threshold %.0f ms, recording cap %.0f s\n",
            g_hold_ms, g_max_seconds);

    g_conflict = legacy_rule_active();
    if (g_conflict)
        fprintf(stderr, "squawkptt: legacy Karabiner Space rule detected — the native "
                        "tap will NOT arm (Space stays normal). Run `squawk migrate` to "
                        "disable the old rule, then this warning clears automatically.\n");

    /* Microphone: request from THIS bundled process so tccd shows the prompt. */
    AVAuthorizationStatus status =
        [AVCaptureDevice authorizationStatusForMediaType:AVMediaTypeAudio];
    fprintf(stderr, "squawkptt: mic authorization status at launch: %ld\n", (long)status);
    if (status == AVAuthorizationStatusNotDetermined) {
        [AVCaptureDevice requestAccessForMediaType:AVMediaTypeAudio
                                 completionHandler:^(BOOL granted) {
            fprintf(stderr, "squawkptt: mic access %s\n", granted ? "granted" : "DENIED");
        }];
    } else if (status != AVAuthorizationStatusAuthorized) {
        fprintf(stderr, "squawkptt: mic access previously denied — enable SquawkPTT in "
                        "System Settings > Privacy & Security > Microphone\n");
    }

    /* Accessibility is required for a listen-and-alter tap and to post the
     * synthetic space. Prompt once; the tap create below still no-ops safely if
     * it's not yet granted. Input Monitoring is prompted by CGEventTapCreate. */
    NSDictionary *opts = @{(__bridge id)kAXTrustedCheckOptionPrompt: @YES};
    BOOL trusted = AXIsProcessTrustedWithOptions((__bridge CFDictionaryRef)opts);
    fprintf(stderr, "squawkptt: accessibility trusted: %s\n", trusted ? "yes" : "no");

    if (install_tap()) {
        fprintf(stderr, "squawkptt: event tap active (Space hold-to-talk in iTerm)\n");
    } else {
        fprintf(stderr, "squawkptt: could not create event tap — grant Accessibility to "
                        "SquawkPTT in System Settings > Privacy & Security > Accessibility "
                        "(and Input Monitoring). Keyboard works normally; retrying…\n");
        /* Retry until the grant lands, so no restart is needed. */
        dispatch_source_t retry = dispatch_source_create(
            DISPATCH_SOURCE_TYPE_TIMER, 0, 0, dispatch_get_main_queue());
        dispatch_source_set_timer(retry, dispatch_time(DISPATCH_TIME_NOW, 3 * NSEC_PER_SEC),
                                  3 * NSEC_PER_SEC, (int64_t)(0.5 * NSEC_PER_SEC));
        dispatch_source_set_event_handler(retry, ^{
            /* Check silently first. On current macOS a CGEventTapCreate that
             * fails for lack of Accessibility raises the "would like to control
             * this Mac" dialog itself — every 3 s from this loop — and that
             * dialog's default button is Deny, so a Return or Space typed while
             * it is up denies the grant. The one prompt at launch is enough. */
            if (!AXIsProcessTrusted())
                return;
            if (install_tap()) {
                fprintf(stderr, "squawkptt: event tap active (Accessibility granted)\n");
                dispatch_source_cancel(retry);
            }
        });
        dispatch_resume(retry);
    }

    /* Every few seconds: look for a Homebrew-upgraded helper (see
     * maybe_self_update), and re-check the legacy conflict so `squawk migrate`
     * clears it live. Cheap: a couple of small file reads. */
    dispatch_source_t recheck = dispatch_source_create(
        DISPATCH_SOURCE_TYPE_TIMER, 0, 0, dispatch_get_main_queue());
    dispatch_source_set_timer(recheck, dispatch_time(DISPATCH_TIME_NOW, 5 * NSEC_PER_SEC),
                              5 * NSEC_PER_SEC, (int64_t)NSEC_PER_SEC);
    dispatch_source_set_event_handler(recheck, ^{
        maybe_self_update();
        BOOL now = legacy_rule_active();
        if (now != g_conflict) {
            g_conflict = now;
            fprintf(stderr, "squawkptt: legacy Karabiner rule %s — native tap %s\n",
                    now ? "reappeared" : "cleared",
                    now ? "disarmed" : "armed");
        }
    });
    dispatch_resume(recheck);

    CFRunLoopRun();
    return 0;
}
