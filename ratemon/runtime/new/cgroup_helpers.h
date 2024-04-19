/* SPDX-License-Identifier: GPL-2.0 */
#ifndef __CGROUP_HELPERS_H
#define __CGROUP_HELPERS_H

#include <errno.h>
#include <string.h>

#define clean_errno() (errno == 0 ? "None" : strerror(errno))
#define log_err(MSG, ...)                                             \
  fprintf(stderr, "(%s:%d: errno: %s) " MSG "\n", __FILE__, __LINE__, \
          clean_errno(), ##__VA_ARGS__)

/* cgroupv2 related */
int cgroup_setup_and_join(const char *path);
int create_and_get_cgroup(const char *path);
unsigned long long get_cgroup_id(const char *path);

int join_cgroup(const char *path);

int setup_cgroup_environment(void);
void cleanup_cgroup_environment(void);

/* cgroupv1 related */
int set_classid(unsigned int id);
int join_classid(void);

int setup_classid_environment(void);
void cleanup_classid_environment(void);

int test__join_cgroup(const char *path);

#endif /* __CGROUP_HELPERS_H */
