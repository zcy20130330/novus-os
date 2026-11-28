/**
 * Novus OS - panic: the one path out of a fatal kernel state.
 *
 * Two callers so far: any ring-0 CPU exception (trap.c) and a B-core that
 * never came online (smp.c). Both used to end in a silent halt loop, which on
 * metal is indistinguishable from a dead machine - a black screen tells the
 * person at the table nothing to write down and tells whoever reads the
 * report nothing either.
 *
 * The sequence is fixed and short: stop interrupts on this core, put the
 * stop code and the state in the serial log and the boot log, paint the blue
 * screen, then park. No allocation, no locks, no scheduler: a panic can be
 * raised from inside any of them.
 */

#include "kernel.h"
#include "panic.h"
#include "msr.h"
#include "usblog.h"
#include "../graphics/nd.h"

/* Other cores keep running their own timer until they notice; this flag is
 * the notice. Each core that ticks after it is set parks itself instead of
 * scheduling, so the machine stops as one machine and not as one core. */
static volatile UINT32 g_kernel_dead;

void kpanic(const char *code, const char *detail) {
  __asm__ volatile("cli");

  g_kernel_dead = 1u;

  kputs("\n[PANIC] ");
  kputs((code != 0) ? code : "KERNEL_PANIC");
  if (detail != 0 && detail[0] != '\0') {
    kputs(": ");
    kputs(detail);
  }
  kputs("\n");
  /* The log is the durable half of the evidence: the screen is a photograph,
   * the file is a file. Force it out before the machine stops touching the
   * medium. */
  usblog_flush(1);

  /* A panic before nd_init() has no framebuffer, and nd_bsod() says so by
   * returning; the serial log is then the whole report. */
  nd_bsod((code != 0) ? code : "KERNEL_PANIC", detail);

  for (;;) {
    cpu_halt();
  }
}

/* Called from the timer path on cores that are not the one that panicked. */
int kpanic_raised(void) { return (int)g_kernel_dead; }
