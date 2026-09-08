/* SPDX-License-Identifier: GPL-2.0 WITH Linux-syscall-note */
#ifndef _UAPI_LINUX_ARGUS_POLICY_H
#define _UAPI_LINUX_ARGUS_POLICY_H

#include <linux/ioctl.h>
#include <linux/types.h>

#define ARGUS_POLICY_ABI_VERSION 2U
#define ARGUS_POLICY_DIGEST_SIZE 32U
#define ARGUS_POLICY_MAX_RULES 16384U

/* Verdict values are shared with the Android framework gate. */
enum argus_policy_verdict {
	ARGUS_VERDICT_ALLOW = 0,
	ARGUS_VERDICT_DENY = 1,
	ARGUS_VERDICT_RESTRICTED = 2,
};

/* The operation is part of the object permission, not part of the subject. */
enum argus_policy_operation {
	ARGUS_OPERATION_UNSPECIFIED = 0,
	ARGUS_OPERATION_CLICK = 1,
	ARGUS_OPERATION_LONG_CLICK = 2,
	ARGUS_OPERATION_TEXT_COMMIT = 3,
	ARGUS_OPERATION_SCROLL = 4,
	ARGUS_OPERATION_GESTURE = 5,
	ARGUS_OPERATION_MAX,
};

/* The paper's MAC subject is Agent; injection modality is not a subject. */
enum argus_policy_subject {
	ARGUS_SUBJECT_AGENT = 2,
};

enum argus_policy_match {
	ARGUS_MATCH_DEFAULT = 0,
	ARGUS_MATCH_EXACT = 1,
};

struct argus_policy_key {
	/* SHA-256 of the canonical DCI+ tuple. */
	__u8 dciplus[ARGUS_POLICY_DIGEST_SIZE];
	__u32 operation;
	__u32 subject;
};

/*
 * target_uid is supplied only by the privileged policy loader.  For a query,
 * the kernel obtains the target UID from current_uid(); an app cannot ask for
 * another app's verdict or substitute a package name.
 */
struct argus_policy_rule {
	__u32 target_uid;
	__u32 verdict;
	struct argus_policy_key key;
};

struct argus_policy_query {
	__u32 abi_version;
	__u32 flags;              /* Must be zero. */
	struct argus_policy_key key;
	__u32 verdict;            /* Kernel output. */
	__u32 match;              /* enum argus_policy_match; kernel output. */
	__u64 generation;         /* Kernel output. */
	/* Nonzero iff verdict is RESTRICTED; identifies the kernel freeze. */
	__u64 restricted_token;
};

/* InputDispatcher asks this before delivering any agent-originated event. */
struct argus_policy_input_check {
	__u32 abi_version;
	__u32 subject;
	__u32 allowed;             /* Kernel output: zero while an agent is frozen. */
	__u32 reserved;
};

/* Only system_server (or CAP_MAC_ADMIN) may end a Restricted freeze. */
struct argus_policy_restricted_resolution {
	__u32 abi_version;
	__u32 target_uid;
	__u64 restricted_token;
	__u32 approved;
	__u32 reserved;
};

#define ARGUS_REPLACE_F_EXPECT_GENERATION (1U << 0)

struct argus_policy_replace {
	__u32 abi_version;
	__u32 flags;
	__u32 default_verdict;
	__u32 rule_count;
	__aligned_u64 rules_ptr;
	__u64 expected_generation;
};

struct argus_policy_info {
	__u32 abi_version;
	__u32 default_verdict;
	__u32 rule_count;
	__u32 reserved;
	__u64 generation;
};

#define ARGUS_POLICY_IOC_MAGIC 0xa7
#define ARGUS_POLICY_IOC_QUERY \
	_IOWR(ARGUS_POLICY_IOC_MAGIC, 0x01, struct argus_policy_query)
#define ARGUS_POLICY_IOC_REPLACE \
	_IOW(ARGUS_POLICY_IOC_MAGIC, 0x02, struct argus_policy_replace)
#define ARGUS_POLICY_IOC_GET_INFO \
	_IOR(ARGUS_POLICY_IOC_MAGIC, 0x03, struct argus_policy_info)
#define ARGUS_POLICY_IOC_INPUT_CHECK \
	_IOWR(ARGUS_POLICY_IOC_MAGIC, 0x04, struct argus_policy_input_check)
#define ARGUS_POLICY_IOC_RESOLVE_RESTRICTED \
	_IOW(ARGUS_POLICY_IOC_MAGIC, 0x05, struct argus_policy_restricted_resolution)

#endif /* _UAPI_LINUX_ARGUS_POLICY_H */
