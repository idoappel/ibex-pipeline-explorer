// Fibonacci + array-sum demo for the pipeline explorer.
// Loop-heavy code: branches, data dependencies and load-use hazards.
#include "simple_system_common.h"

int main(void) {
  pcount_enable(0);
  pcount_reset();
  pcount_enable(1);

  puts("Fibonacci test\n");

  // fibonacci with a loop (branches + back-to-back data dependencies)
  volatile int n = 12;
  int a = 0, b = 1;
  for (int i = 0; i < n; i++) {
    int t = a + b;
    a = b;
    b = t;
  }
  puts("fib(12) = ");
  puthex(a);            // expect 144 = 0x90
  putchar('\n');

  // array sum (memory loads + load-use hazards)
  static volatile int arr[8] = {1, 2, 3, 4, 5, 6, 7, 8};
  int sum = 0;
  for (int i = 0; i < 8; i++)
    sum += arr[i];
  puts("sum(1..8) = ");
  puthex(sum);          // expect 36 = 0x24
  putchar('\n');

  if (a == 144 && sum == 36)
    puts("PASS\n");
  else
    puts("FAIL\n");

  pcount_enable(0);
  return 0;
}
