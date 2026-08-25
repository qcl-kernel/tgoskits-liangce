cmake_minimum_required(VERSION 3.20.0)

find_package(Zephyr REQUIRED HINTS $ENV{ZEPHYR_BASE})
project(contest_zephyr_control)

target_sources(app PRIVATE
  src/main.c
  src/icpc/icpc.c
  src/control_mailbox.c
  src/control_loop.c
  src/plant.c
  src/watchdog.c
)

zephyr_include_directories(include)
zephyr_include_directories(src)
