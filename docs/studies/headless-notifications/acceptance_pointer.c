/* Optional host-acceptance actuator, never installed by remote-chrome.
 * Build with wayland-scanner's client header/private code for the upstream
 * wlr-virtual-pointer-unstable-v1.xml and pkg-config wayland-client flags.
 * Inspect a screenshot and verify the test notification's bounds before use.
 * Coordinates are relative to the first compositor output. No input is sent
 * until argument bounds and required interfaces have been checked. The caller
 * must verify output order, size, scale, and popup coordinates; this tool does
 * not discover dimensions or select a named output.
 */
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <wayland-client.h>
#include "virtual-pointer-client.h"

static struct zwlr_virtual_pointer_manager_v1 *manager;
static struct wl_output *output;

static void geometry(void *data, struct wl_output *object, int32_t x, int32_t y,
                     int32_t width, int32_t height, int32_t subpixel,
                     const char *make, const char *model, int32_t transform) {
    (void)data; (void)object; (void)x; (void)y; (void)width; (void)height;
    (void)subpixel; (void)make; (void)model; (void)transform;
}

static void mode(void *data, struct wl_output *object, uint32_t flags,
                 int32_t width, int32_t height, int32_t refresh) {
    (void)data; (void)object; (void)flags; (void)width; (void)height; (void)refresh;
}

static const struct wl_output_listener output_listener = {
    .geometry = geometry, .mode = mode
};

static void global(void *data, struct wl_registry *registry, uint32_t id,
                   const char *interface, uint32_t version) {
    (void)data;
    if (!strcmp(interface, "zwlr_virtual_pointer_manager_v1") && version >= 2)
        manager = wl_registry_bind(registry, id,
                  &zwlr_virtual_pointer_manager_v1_interface, 2);
    if (!output && !strcmp(interface, "wl_output")) {
        output = wl_registry_bind(registry, id, &wl_output_interface, 1);
        wl_output_add_listener(output, &output_listener, NULL);
    }
}

static void removed(void *data, struct wl_registry *registry, uint32_t id) {
    (void)data; (void)registry; (void)id;
}

int main(int argc, char **argv) {
    if (argc != 5) {
        fprintf(stderr, "usage: acceptance-pointer X Y WIDTH HEIGHT\n");
        return 2;
    }
    uint32_t values[4];
    for (int i = 0; i < 4; i++) {
        char *end;
        unsigned long value = strtoul(argv[i + 1], &end, 10);
        if (!*argv[i + 1] || *end || value > 32768) return 2;
        values[i] = (uint32_t)value;
    }
    if (!values[2] || !values[3] || values[0] >= values[2] || values[1] >= values[3])
        return 2;
    struct wl_display *display = wl_display_connect(NULL);
    if (!display) return 3;
    struct wl_registry *registry = wl_display_get_registry(display);
    const struct wl_registry_listener listener = {global, removed};
    wl_registry_add_listener(registry, &listener, NULL);
    if (wl_display_roundtrip(display) < 0 || !manager || !output) return 3;
    struct zwlr_virtual_pointer_v1 *pointer =
        zwlr_virtual_pointer_manager_v1_create_virtual_pointer_with_output(manager, NULL, output);
    struct timespec now;
    clock_gettime(CLOCK_MONOTONIC, &now);
    uint32_t stamp = (uint32_t)(now.tv_sec * 1000 + now.tv_nsec / 1000000);
    zwlr_virtual_pointer_v1_motion_absolute(pointer, stamp, values[0], values[1], values[2], values[3]);
    zwlr_virtual_pointer_v1_frame(pointer);
    if (wl_display_roundtrip(display) < 0) return 3;
    zwlr_virtual_pointer_v1_button(pointer, stamp + 1, 0x110, WL_POINTER_BUTTON_STATE_PRESSED);
    zwlr_virtual_pointer_v1_frame(pointer);
    zwlr_virtual_pointer_v1_button(pointer, stamp + 2, 0x110, WL_POINTER_BUTTON_STATE_RELEASED);
    zwlr_virtual_pointer_v1_frame(pointer);
    int failed = wl_display_roundtrip(display) < 0;
    zwlr_virtual_pointer_v1_destroy(pointer);
    zwlr_virtual_pointer_manager_v1_destroy(manager);
    wl_proxy_destroy((struct wl_proxy *)output);
    wl_registry_destroy(registry);
    wl_display_disconnect(display);
    return failed ? 3 : 0;
}
