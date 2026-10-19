/* SPDX-License-Identifier: GPL-2.0-or-later */
/*
 * flapsim POC: virtual split-flap modules behind the picsimlab I2C hook.
 *
 * Each module = PCF8575 16-bit I/O expander + 28BYJ-48 stepper on P01..P04
 * + character drum + hall sensor on P17 (bit 15). The model knows nothing
 * about the firmware's settings; it only knows the physical hardware.
 *
 * Config via environment:
 *   FLAPSIM_BUS0 / FLAPSIM_BUS1  comma list of hex addresses (default bus0 0x20-0x27, bus1 none)
 *   FLAPSIM_STEPS                steps per drum rotation (default 2048)
 *   FLAPSIM_MAGNET               physical step where the hall sensor sees the magnet (default 730)
 *   FLAPSIM_WINDOW               steps the sensor stays active (default 40)
 *   FLAPSIM_SEED                 seed for random start angles (default 1; 0 = all start at 0)
 *   FLAPSIM_TRACE                1 = log every I2C transaction
 *   FLAPSIM_STATE                path; drum state JSON is rewritten here on every motor stop
 */
#include "qemu/timer.h"

#define FS_MAX_BUS 2
#define FS_NFLAPS 37

static const char fs_flaps[FS_NFLAPS + 1] = " ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789";

typedef struct {
    int present;
    uint16_t out;       /* last value written (PCF8575 latch) */
    uint8_t wbuf[2];
    int widx, ridx;
    int phase;          /* rotor phase 0..3, -1 = unknown/unpowered */
    int angle;          /* physical drum position in steps */
    long steps_fwd, steps_back, stalls, writes, reads;
    int energized;
    int last_hall_read;
} FsModule;

static FsModule fs_mod[FS_MAX_BUS][128];
static int fs_inited, fs_steps = 2048, fs_magnet = 730, fs_window = 40, fs_trace;
static const char *fs_state_path, *fs_timeline_path;
static FILE *fs_timeline;
static double fs_last_snap = -1;

static double fs_now(void) { return qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL) / 1e9; }

static void fs_parse_bus(int bus, const char *def) {
    char env[16];
    snprintf(env, sizeof env, "FLAPSIM_BUS%d", bus);
    const char *v = getenv(env);
    if (!v) v = def;
    char *dup = g_strdup(v), *save = NULL;
    for (char *t = strtok_r(dup, ",", &save); t; t = strtok_r(NULL, ",", &save)) {
        long a = strtol(t, NULL, 16);
        if (a > 0 && a < 128) fs_mod[bus][a].present = 1;
    }
    g_free(dup);
}

static void fs_init(void) {
    if (fs_inited) return;
    fs_inited = 1;
    if (getenv("FLAPSIM_STEPS")) fs_steps = atoi(getenv("FLAPSIM_STEPS"));
    if (getenv("FLAPSIM_MAGNET")) fs_magnet = atoi(getenv("FLAPSIM_MAGNET"));
    if (getenv("FLAPSIM_WINDOW")) fs_window = atoi(getenv("FLAPSIM_WINDOW"));
    fs_trace = getenv("FLAPSIM_TRACE") && atoi(getenv("FLAPSIM_TRACE"));
    fs_state_path = getenv("FLAPSIM_STATE");
    fs_timeline_path = getenv("FLAPSIM_TIMELINE");
    if (fs_timeline_path) fs_timeline = fopen(fs_timeline_path, "w");
    unsigned seed = getenv("FLAPSIM_SEED") ? (unsigned) atoi(getenv("FLAPSIM_SEED")) : 1;
    fs_parse_bus(0, "20,21,22,23,24,25,26,27");
    fs_parse_bus(1, "");
    srand(seed);
    for (int b = 0; b < FS_MAX_BUS; b++)
        for (int a = 0; a < 128; a++) {
            FsModule *m = &fs_mod[b][a];
            m->phase = -1;
            m->angle = seed ? rand() % fs_steps : 0;
            if (m->present)
                fprintf(stderr, "[flapsim] bus%d 0x%02x present, drum starts at step %d\n", b, a, m->angle);
        }
}

static char fs_shown(const FsModule *m) {
    /* Flap i is centred on physical step i*steps/37; the nearest flap is the one showing. */
    int idx = (int) (((long) m->angle * FS_NFLAPS * 2 + fs_steps) / (2L * fs_steps)) % FS_NFLAPS;
    return fs_flaps[idx];
}

static int fs_hall(const FsModule *m) {
    int d = (m->angle - fs_magnet + fs_steps) % fs_steps;
    return d < fs_window;
}

/* Coils on P01..P04, driven high = energized. Map the energized pair to a rotor phase. */
static int fs_coil_phase(uint16_t out) {
    switch ((out >> 1) & 0xF) {
    case 0x3: return 0; /* P01+P02 */
    case 0x9: return 1; /* P01+P04 */
    case 0xC: return 2; /* P03+P04 */
    case 0x6: return 3; /* P02+P03 */
    default: return -1; /* off or not a valid full-step pair */
    }
}

static void fs_dump_state(void) {
    if (!fs_state_path) return;
    FILE *f = fopen(fs_state_path, "w");
    if (!f) return;
    fprintf(f, "{\"t\":%.3f,\"modules\":[", fs_now());
    int first = 1;
    for (int b = 0; b < FS_MAX_BUS; b++)
        for (int a = 0; a < 128; a++) {
            FsModule *m = &fs_mod[b][a];
            if (!m->present) continue;
            fprintf(f, "%s{\"bus\":%d,\"addr\":%d,\"angle\":%d,\"char\":\"%c\",\"energized\":%d}",
                    first ? "" : ",", b, a, m->angle, fs_shown(m), m->energized);
            first = 0;
        }
    fprintf(f, "]}\n");
    fclose(f);
}

static void fs_snapshot(const char *event) {
    if (!fs_timeline) return;
    fprintf(fs_timeline, "{\"t\":%.3f,\"wall\":%.3f,\"event\":\"%s\",\"modules\":[", fs_now(),
            g_get_monotonic_time() / 1e6, event);
    int first = 1;
    for (int b = 0; b < FS_MAX_BUS; b++)
        for (int a = 0; a < 128; a++) {
            FsModule *m = &fs_mod[b][a];
            if (!m->present) continue;
            fprintf(fs_timeline, "%s[%d,%d,%d,\"%c\",%d,%d]", first ? "" : ",", b, a, m->angle, fs_shown(m),
                    m->energized, fs_hall(m));
            first = 0;
        }
    fprintf(fs_timeline, "]}\n");
    fflush(fs_timeline);
}

static void fs_report_display(const char *why) {
    char row[2][129];
    int n[2] = {0, 0};
    for (int b = 0; b < FS_MAX_BUS; b++)
        for (int a = 0; a < 128; a++)
            if (fs_mod[b][a].present) row[b][n[b]++] = fs_shown(&fs_mod[b][a]);
    row[0][n[0]] = row[1][n[1]] = 0;
    fprintf(stderr, "[flapsim] t=%8.3fs %-14s display: [%s]%s%s%s\n", fs_now(), why, row[0],
            n[1] ? " [" : "", row[1], n[1] ? "]" : "");
    for (int b = 0; b < FS_MAX_BUS; b++)
        for (int a = 0; a < 128; a++) {
            FsModule *m = &fs_mod[b][a];
            if (m->present)
                fprintf(stderr, "[flapsim]   bus%d 0x%02x angle=%4d fwd=%ld back=%ld stalls=%ld writes=%ld reads=%ld\n", b, a,
                        m->angle, m->steps_fwd, m->steps_back, m->stalls, m->writes, m->reads);
        }
    fs_dump_state();
    fs_snapshot(why);
}

static void fs_apply_output(int bus, int addr, FsModule *m, uint16_t out) {
    int was_energized = m->energized;
    m->out = out;
    m->writes++;
    int p = fs_coil_phase(out);
    m->energized = (p >= 0);
    if (was_energized != m->energized && getenv("FLAPSIM_LOG_HALL"))
        fprintf(stderr, "[flapsim] t=%8.3fs bus%d 0x%02x motor %s at angle %d (fwd %ld)\n", fs_now(), bus, addr,
                m->energized ? "ON " : "OFF", m->angle, m->steps_fwd);
    if (p < 0) {
        m->phase = -1;
    } else if (m->phase < 0) {
        m->phase = p; /* rotor snaps to the newly energized phase; ignore sub-step alignment */
    } else {
        int d = (p - m->phase + 4) % 4;
        if (d == 1) {
            m->angle = (m->angle + 1) % fs_steps;
            m->steps_fwd++;
        } else if (d == 3) {
            m->angle = (m->angle - 1 + fs_steps) % fs_steps;
            m->steps_back++;
        } else if (d == 2) {
            m->stalls++; /* opposite phase: real motor would stall or jump unpredictably */
        }
        m->phase = p;
    }
    if (fs_now() - fs_last_snap >= 0.1) {
        fs_last_snap = fs_now();
        fs_snapshot("tick");
    }
    if (was_energized && !m->energized) {
        /* A move is finished once every module has released its coils. */
        int any = 0;
        for (int b = 0; b < FS_MAX_BUS && !any; b++)
            for (int a = 0; a < 128 && !any; a++) any = fs_mod[b][a].present && fs_mod[b][a].energized;
        if (!any) fs_report_display("motors off");
    }
}

static int flapsim_i2c_event(const uint8_t id, const uint8_t addr, const uint16_t event) {
    fs_init();
    if (id >= FS_MAX_BUS || addr >= 128) return 1;
    FsModule *m = &fs_mod[id][addr];
    uint8_t ev = event & 0xFF;
    static int open_addr[FS_MAX_BUS] = {-1, -1};
    static long anomalies;

    if (ev <= 2) {
        if (open_addr[id] >= 0)
            fprintf(stderr, "[flapsim] ANOMALY#%ld t=%8.3fs bus%d START 0x%02x while 0x%02x still open\n", ++anomalies,
                    fs_now(), id, addr, open_addr[id]);
        open_addr[id] = addr;
    } else if (ev == 3) {
        if (m->widx != 0 && m->widx != 2)
            fprintf(stderr, "[flapsim] ANOMALY#%ld t=%8.3fs bus%d 0x%02x write of %d bytes\n", ++anomalies, fs_now(), id,
                    addr, m->widx);
        if (m->ridx != 0 && m->ridx != 2)
            fprintf(stderr, "[flapsim] ANOMALY#%ld t=%8.3fs bus%d 0x%02x read of %d bytes\n", ++anomalies, fs_now(), id,
                    addr, m->ridx);
        open_addr[id] = -1;
    }

    switch (ev) {
    case 0: /* I2C_START_RECV */
    case 1: /* I2C_START_SEND */
    case 2: /* I2C_START_SEND_ASYNC */
        if (fs_trace || !m->present)
            fprintf(stderr, "[flapsim] t=%8.3fs bus%d 0x%02x START %s -> %s\n", fs_now(), id, addr,
                    ev == 0 ? "read" : "write", m->present ? "ACK" : "NACK");
        m->widx = m->ridx = 0;
        return m->present ? 0 : 1;
    case 3: /* I2C_FINISH */
        if (m->present && m->widx == 2) {
            uint16_t out = m->wbuf[0] | (m->wbuf[1] << 8);
            if (fs_trace)
                fprintf(stderr, "[flapsim] t=%8.3fs bus%d 0x%02x WRITE 0x%04x coils=%x\n", fs_now(), id, addr,
                        out, (out >> 1) & 0xF);
            fs_apply_output(id, addr, m, out);
        }
        m->widx = m->ridx = 0;
        return 0;
    case 4: /* I2C_NACK */
        return 0;
    case 5: { /* data byte written by the ESP32 */
        uint8_t data = event >> 8;
        if (m->widx < 2) m->wbuf[m->widx] = data;
        m->widx++;
        return 0;
    }
    case 6: { /* ESP32 reads a byte */
        /* Quasi-bidirectional pins read back their latch, except P17 which the hall sensor drives. */
        uint16_t in = (m->out & 0x7FFF) | (fs_hall(m) ? 0x8000 : 0);
        uint8_t b = m->ridx == 0 ? (in & 0xFF) : (in >> 8);
        m->ridx++;
        if (m->ridx == 2) {
            m->reads++;
            int h = fs_hall(m);
            if (h && !m->last_hall_read && getenv("FLAPSIM_LOG_HALL"))
                fprintf(stderr, "[flapsim] t=%8.3fs bus%d 0x%02x firmware sees magnet at angle %d (fwd steps so far %ld)\n",
                        fs_now(), id, addr, m->angle, m->steps_fwd);
            m->last_hall_read = h;
        }
        if (fs_trace && m->ridx == 2)
            fprintf(stderr, "[flapsim] t=%8.3fs bus%d 0x%02x READ  0x%04x hall=%d angle=%d\n", fs_now(), id, addr,
                    in, fs_hall(m), m->angle);
        return b;
    }
    }
    return 0;
}
