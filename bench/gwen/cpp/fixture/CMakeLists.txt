cmake_minimum_required(VERSION 3.24)
project(minilib CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)
set(CMAKE_COMPILE_WARNING_AS_ERROR ON)
add_compile_options(-Wall -Wextra -Wpedantic)

add_library(minilib_core
  src/paging.cpp
  src/textstats.cpp
  src/inventory.cpp
  src/cli.cpp
)
target_include_directories(minilib_core PUBLIC include)

add_executable(minilib src/main.cpp)
target_link_libraries(minilib PRIVATE minilib_core)

enable_testing()
find_package(GTest REQUIRED)
include(GoogleTest)

# Tests read data/ through MINILIB_SOURCE_DIR, not through the working directory.
function(add_minilib_tests target label)
  add_executable(${target} ${ARGN})
  target_link_libraries(${target} PRIVATE minilib_core GTest::gtest_main)
  target_compile_definitions(${target} PRIVATE "MINILIB_SOURCE_DIR=\"${CMAKE_CURRENT_SOURCE_DIR}\"")
  gtest_discover_tests(${target} PROPERTIES LABELS ${label})
endfunction()

file(GLOB VISIBLE_TESTS CONFIGURE_DEPENDS tests/*.cpp)
add_minilib_tests(minilib_visible_tests visible ${VISIBLE_TESTS})

# Hidden tests stay out of the default build: a hidden test that does not compile fails only the build-hidden step.
if(IS_DIRECTORY ${CMAKE_CURRENT_SOURCE_DIR}/tests/hidden)
  file(GLOB HIDDEN_TESTS CONFIGURE_DEPENDS tests/hidden/*.cpp)
  add_minilib_tests(minilib_hidden_tests hidden ${HIDDEN_TESTS})
  set_target_properties(minilib_hidden_tests PROPERTIES EXCLUDE_FROM_ALL TRUE)
endif()
