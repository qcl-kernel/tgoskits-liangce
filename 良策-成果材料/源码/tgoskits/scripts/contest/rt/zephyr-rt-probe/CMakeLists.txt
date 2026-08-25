cmake_minimum_required(VERSION 3.20.0)

find_package(Zephyr REQUIRED HINTS $ENV{ZEPHYR_BASE})
project(p3_zephyr_rt_probe)

function(p3_safe_string name default_value)
  if(NOT DEFINED ${name})
    set(${name} "${default_value}" CACHE STRING "P3 realtime probe ${name}")
  endif()
  if(NOT "${${name}}" MATCHES "^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
    message(FATAL_ERROR "${name} must be a safe P3 identifier")
  endif()
endfunction()

p3_safe_string(P3_RUN_ID "p3-native-probe")
p3_safe_string(P3_SCENARIO "RT-NATIVE")
p3_safe_string(P3_CLOCK_DOMAIN "native-guest-counter")
p3_safe_string(P3_CLOCK_MAPPING_ID "native-guest-clock")
p3_safe_string(P3_PATH_VARIANT "native")
p3_safe_string(P3_PATH_ID "guest-period")

if(NOT DEFINED P3_FEATURE_STATE)
  set(P3_FEATURE_STATE "off" CACHE STRING "P3 feature state")
endif()
if(NOT P3_FEATURE_STATE MATCHES "^(off|on)$")
  message(FATAL_ERROR "P3_FEATURE_STATE must be off or on")
endif()

foreach(field P3_VM_ID_JSON P3_VCPU_ID_JSON P3_PCPU_ID_JSON)
  if(NOT DEFINED ${field})
    set(${field} "null" CACHE STRING "P3 optional numeric identity")
  endif()
  if(NOT "${${field}}" MATCHES "^(null|[0-9]+)$")
    message(FATAL_ERROR "${field} must be null or a non-negative integer")
  endif()
endforeach()

if(NOT DEFINED P3_CNTVOFF_TICKS)
  set(P3_CNTVOFF_TICKS "0" CACHE STRING "P3 guest-to-host counter offset")
endif()
if(NOT P3_CNTVOFF_TICKS MATCHES "^-?[0-9]+$")
  message(FATAL_ERROR "P3_CNTVOFF_TICKS must be a signed integer")
endif()

if(NOT DEFINED P3_SAMPLE_COUNT)
  set(P3_SAMPLE_COUNT "100000" CACHE STRING "P3 samples in one run")
endif()
if(NOT P3_SAMPLE_COUNT MATCHES "^[1-9][0-9]*$")
  message(FATAL_ERROR "P3_SAMPLE_COUNT must be a positive integer")
endif()

configure_file(
  ${CMAKE_CURRENT_SOURCE_DIR}/src/probe_config.h.in
  ${CMAKE_CURRENT_BINARY_DIR}/generated/p3_probe_config.h
  @ONLY
)

target_sources(app PRIVATE src/main.c)
target_include_directories(app PRIVATE ${CMAKE_CURRENT_BINARY_DIR}/generated)
