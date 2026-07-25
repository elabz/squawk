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

#include <limits.h>
#include <signal.h>
#include <spawn.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

extern char **environ;

/* Virtual keycode for Space (Carbon kVK_Space). */
#define KEYCODE_SPACE 49

/* Stamped onto the synthetic space we post for a tap, so the tap recognizes
 * and passes through its own events instead of recursing on them. */
#define SQUAWK_EVENT_MARKER 0x5157414B /* "QWAK" */

/* Default hold threshold in ms — matches the tuned Karabiner value. */
#define DEFAULT_HOLD_MS 400.0

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

/* Set when the legacy Karabiner Space rule is still present: the tap refuses to
 * arm so Space is never grabbed by two owners at once. Re-checked periodically
 * so running `squawk migrate` clears it without restarting the helper. */
static BOOL g_conflict = NO;

/* Fire-and-forget: spawn `squawk <subcommand>` and return immediately. Never
 * block the event-tap callback — a slow callback trips kCGEventTapDisabledBy-
 * Timeout. squawk itself detaches sox and the transcription worker. SIGCHLD is
 * SIG_IGN (set in main), so the kernel reaps these with no zombies. */
static void run_squawk(const char *subcommand) {
    if (!squawk_path[0]) {
        fprintf(stderr, "squawkptt: cannot run `squawk %s` — no CLI was found at "
                        "startup. Run `squawk install-agent` to record its path.\n",
                subcommand);
        return;
    }
    char *argv[] = {squawk_path, (char *)subcommand, NULL};
    pid_t pid;
    if (posix_spawn(&pid, squawk_path, NULL, NULL, argv, environ) != 0)
        perror("squawkptt: posix_spawn squawk");
}

/* Post a genuine Space keypress (down+up) for a tap we swallowed, marked so the
 * tap ignores it. */
static void synthesize_space(void) {
    CGEventSourceRef src = CGEventSourceCreate(kCGEventSourceStateHIDSystemState);
    CGEventRef down = CGEventCreateKeyboardEvent(src, (CGKeyCode)KEYCODE_SPACE, true);
    CGEventRef up = CGEventCreateKeyboardEvent(src, (CGKeyCode)KEYCODE_SPACE, false);
    CGEventSetIntegerValueField(down, kCGEventSourceUserData, SQUAWK_EVENT_MARKER);
    CGEventSetIntegerValueField(up, kCGEventSourceUserData, SQUAWK_EVENT_MARKER);
    CGEventPost(kCGHIDEventTap, down);
    CGEventPost(kCGHIDEventTap, up);
    if (down) CFRelease(down);
    if (up) CFRelease(up);
    if (src) CFRelease(src);
}

static BOOL frontmost_is_iterm(void) {
    NSRunningApplication *app = [[NSWorkspace sharedWorkspace] frontmostApplication];
    return [app.bundleIdentifier isEqualToString:@"com.googlecode.iterm2"];
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

static CGEventRef tap_callback(CGEventTapProxy proxy, CGEventType type,
                               CGEventRef event, void *refcon) {
    (void)proxy;
    (void)refcon;

    /* macOS disabled the tap (our callback was too slow, or user input raced):
     * re-enable it so hold-to-talk keeps working without a restart. */
    if (type == kCGEventTapDisabledByTimeout ||
        type == kCGEventTapDisabledByUserInput) {
        if (g_tap)
            CGEventTapEnable(g_tap, true);
        return event;
    }

    if (type != kCGEventKeyDown && type != kCGEventKeyUp)
        return event;

    int64_t keycode = CGEventGetIntegerValueField(event, kCGKeyboardEventKeycode);
    if (keycode != KEYCODE_SPACE)
        return event; /* every non-Space key passes through untouched */

    /* Ignore the synthetic space we post for taps, so we never recurse. */
    if (CGEventGetIntegerValueField(event, kCGEventSourceUserData) == SQUAWK_EVENT_MARKER)
        return event;

    /* Only alter Space in iTerm, and never while the legacy rule could also be
     * grabbing it. Any other context is passed through completely unmodified. */
    if (g_conflict || !frontmost_is_iterm()) {
        /* Reset any half-started hold if focus left iTerm mid-press. */
        g_armed = NO;
        g_recording = NO;
        return event;
    }

    if (type == kCGEventKeyDown) {
        /* Auto-repeat while armed or recording: swallow so a held Space never
         * spews spaces. */
        if (g_armed || g_recording)
            return NULL;

        g_armed = YES;
        uint64_t gen = ++g_hold_gen;
        dispatch_after(dispatch_time(DISPATCH_TIME_NOW, (int64_t)(g_hold_ms * NSEC_PER_MSEC)),
                       dispatch_get_main_queue(), ^{
            /* Still the same, still-held press? Then it's a hold: start. */
            if (g_armed && !g_recording && gen == g_hold_gen) {
                g_armed = NO;
                g_recording = YES;
                run_squawk("ptt-start");
            }
        });
        return NULL; /* swallow the down; we decide tap-vs-hold on release/timer */
    }

    /* keyUp */
    if (g_recording) {
        g_recording = NO;
        g_hold_gen++;
        run_squawk("ptt-stop");
        return NULL;
    }
    if (g_armed) {
        /* Released before the threshold → a tap. Cancel the pending timer (via
         * the generation bump) and emit a real space. */
        g_armed = NO;
        g_hold_gen++;
        synthesize_space();
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
static double read_hold_ms(void) {
    const char *env = getenv("SQUAWK_HOLD_MS");
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
            while (fgets(line, sizeof line, f)) {
                if (strncmp(line, "SQUAWK_HOLD_MS=", 15) == 0) {
                    double v = atof(line + 15);
                    if (v > 0)
                        found = v;
                }
            }
            fclose(f);
            if (found > 0)
                return found;
        }
    }
    return DEFAULT_HOLD_MS;
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

    /* Non-blocking child spawns with no zombies: let the kernel reap them. */
    signal(SIGCHLD, SIG_IGN);

    g_hold_ms = read_hold_ms();
    fprintf(stderr, "squawkptt: hold threshold %.0f ms\n", g_hold_ms);

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
            if (install_tap()) {
                fprintf(stderr, "squawkptt: event tap active (Accessibility granted)\n");
                dispatch_source_cancel(retry);
            }
        });
        dispatch_resume(retry);
    }

    /* Re-check the legacy conflict periodically so `squawk migrate` clears it
     * live. Cheap: a small file read every few seconds. */
    dispatch_source_t recheck = dispatch_source_create(
        DISPATCH_SOURCE_TYPE_TIMER, 0, 0, dispatch_get_main_queue());
    dispatch_source_set_timer(recheck, dispatch_time(DISPATCH_TIME_NOW, 5 * NSEC_PER_SEC),
                              5 * NSEC_PER_SEC, (int64_t)NSEC_PER_SEC);
    dispatch_source_set_event_handler(recheck, ^{
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
