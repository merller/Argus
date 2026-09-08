// SPDX-License-Identifier: GPL-2.0
/*
 * Argus kernel policy map.
 *
 * This module intentionally does not parse UI text or signed manifests.  A
 * trusted userspace monitor verifies and compiles a manifest, resolves package
 * names to Android UIDs, and atomically installs exact DCI+ rules.  Runtime
 * callers can only query rules for current_uid().
 */

#include <linux/capability.h>
#include <linux/cred.h>
#include <linux/errno.h>
#include <linux/fs.h>
#include <linux/hashtable.h>
#include <linux/init.h>
#include <linux/jhash.h>
#include <linux/miscdevice.h>
#include <linux/module.h>
#include <linux/mutex.h>
#include <linux/overflow.h>
#include <linux/rcupdate.h>
#include <linux/random.h>
#include <linux/slab.h>
#include <linux/string.h>
#include <linux/uaccess.h>
#include <linux/jiffies.h>
#include <linux/user_namespace.h>

#include <linux/argus_policy.h>

#define ARGUS_POLICY_HASH_BITS 12

struct argus_policy_node {
	struct hlist_node hnode;
	struct argus_policy_rule rule;
};

struct argus_policy_table {
	DECLARE_HASHTABLE(rules, ARGUS_POLICY_HASH_BITS);
	struct rcu_head rcu;
	u32 default_verdict;
	u32 rule_count;
	u64 generation;
};

static DEFINE_MUTEX(argus_update_lock);
static struct argus_policy_table __rcu *argus_active_table;

#define ARGUS_SYSTEM_UID 1000U
#define ARGUS_RESTRICTED_FAILSAFE_SECONDS 35U

/* Only one operation can own the global agent freeze at a time. */
struct argus_restricted_state {
	bool active;
	u32 target_uid;
	u64 token;
	unsigned long expires;
};

static DEFINE_MUTEX(argus_restricted_lock);
static struct argus_restricted_state argus_restricted;

static void argus_expire_restricted_locked(void)
{
	if (argus_restricted.active &&
	    time_after_eq(jiffies, argus_restricted.expires))
		memset(&argus_restricted, 0, sizeof(argus_restricted));
}

static u64 argus_begin_restricted(uid_t target_uid)
{
	u64 token;

	mutex_lock(&argus_restricted_lock);
	argus_expire_restricted_locked();
	if (argus_restricted.active) {
		token = argus_restricted.target_uid == target_uid
			? argus_restricted.token : 0;
		mutex_unlock(&argus_restricted_lock);
		return token;
	}
	do {
		token = get_random_u64();
	} while (!token);
	argus_restricted.active = true;
	argus_restricted.target_uid = target_uid;
	argus_restricted.token = token;
	argus_restricted.expires = jiffies +
		ARGUS_RESTRICTED_FAILSAFE_SECONDS * HZ;
	mutex_unlock(&argus_restricted_lock);
	return token;
}

static bool argus_verdict_valid(u32 verdict)
{
	return verdict == ARGUS_VERDICT_ALLOW ||
	       verdict == ARGUS_VERDICT_DENY ||
	       verdict == ARGUS_VERDICT_RESTRICTED;
}

static bool argus_subject_valid(u32 subject)
{
	return subject == ARGUS_SUBJECT_AGENT;
}

static bool argus_digest_nonzero(const u8 *digest)
{
	size_t i;

	for (i = 0; i < ARGUS_POLICY_DIGEST_SIZE; ++i) {
		if (digest[i] != 0)
			return true;
	}
	return false;
}

static bool argus_key_valid(const struct argus_policy_key *key)
{
	return argus_digest_nonzero(key->dciplus) &&
	       key->operation > ARGUS_OPERATION_UNSPECIFIED &&
	       key->operation < ARGUS_OPERATION_MAX &&
	       argus_subject_valid(key->subject);
}

static u32 argus_key_hash(u32 target_uid, const struct argus_policy_key *key)
{
	u32 hash = jhash(&target_uid, sizeof(target_uid), 0x41524755U);

	return jhash(key, sizeof(*key), hash);
}

static bool argus_rule_matches(const struct argus_policy_rule *rule,
			       u32 target_uid,
			       const struct argus_policy_key *key)
{
	return rule->target_uid == target_uid &&
	       rule->key.operation == key->operation &&
	       rule->key.subject == key->subject &&
	       !memcmp(rule->key.dciplus, key->dciplus,
		       ARGUS_POLICY_DIGEST_SIZE);
}

static struct argus_policy_node *
argus_find_node(struct argus_policy_table *table, u32 target_uid,
		const struct argus_policy_key *key)
{
	struct argus_policy_node *node;
	u32 hash = argus_key_hash(target_uid, key);

	hash_for_each_possible(table->rules, node, hnode, hash) {
		if (argus_rule_matches(&node->rule, target_uid, key))
			return node;
	}
	return NULL;
}

static struct argus_policy_node *
argus_find_node_rcu(struct argus_policy_table *table, u32 target_uid,
		    const struct argus_policy_key *key)
{
	struct argus_policy_node *node;
	u32 hash = argus_key_hash(target_uid, key);

	hash_for_each_possible_rcu(table->rules, node, hnode, hash) {
		if (argus_rule_matches(&node->rule, target_uid, key))
			return node;
	}
	return NULL;
}

static struct argus_policy_table *argus_table_alloc(u32 default_verdict)
{
	struct argus_policy_table *table;

	table = kzalloc(sizeof(*table), GFP_KERNEL);
	if (!table)
		return NULL;
	hash_init(table->rules);
	table->default_verdict = default_verdict;
	return table;
}

static void argus_table_free(struct argus_policy_table *table)
{
	struct argus_policy_node *node;
	struct hlist_node *tmp;
	int bucket;

	if (!table)
		return;
	hash_for_each_safe(table->rules, bucket, tmp, node, hnode) {
		hash_del(&node->hnode);
		kfree(node);
	}
	kfree(table);
}

static int argus_query(void __user *argp)
{
	struct argus_policy_query query;
	struct argus_policy_table *table;
	struct argus_policy_node *node;
	uid_t target_uid;

	if (copy_from_user(&query, argp, sizeof(query)))
		return -EFAULT;
	if (query.abi_version != ARGUS_POLICY_ABI_VERSION || query.flags != 0 ||
	    !argus_key_valid(&query.key))
		return -EINVAL;

	target_uid = from_kuid(&init_user_ns, current_uid());
	if (target_uid == (uid_t)-1)
		return -EOVERFLOW;

	rcu_read_lock();
	table = rcu_dereference(argus_active_table);
	if (unlikely(!table)) {
		rcu_read_unlock();
		return -ENODEV;
	}

	query.verdict = table->default_verdict;
	query.match = ARGUS_MATCH_DEFAULT;
	query.generation = table->generation;
	query.restricted_token = 0;

	node = argus_find_node_rcu(table, target_uid, &query.key);
	if (node) {
		query.verdict = node->rule.verdict;
		query.match = ARGUS_MATCH_EXACT;
	}
	rcu_read_unlock();
	if (query.verdict == ARGUS_VERDICT_RESTRICTED) {
		query.restricted_token = argus_begin_restricted(target_uid);
		if (!query.restricted_token)
			return -EBUSY;
	}

	if (!argus_verdict_valid(query.verdict))
		return -EUCLEAN;
	if (copy_to_user(argp, &query, sizeof(query)))
		return -EFAULT;
	return 0;
}

static int argus_input_check(void __user *argp)
{
	struct argus_policy_input_check check;

	if (copy_from_user(&check, argp, sizeof(check)))
		return -EFAULT;
	if (check.abi_version != ARGUS_POLICY_ABI_VERSION || check.reserved ||
	    !argus_subject_valid(check.subject))
		return -EINVAL;

	mutex_lock(&argus_restricted_lock);
	argus_expire_restricted_locked();
	check.allowed = argus_restricted.active ? 0 : 1;
	mutex_unlock(&argus_restricted_lock);
	if (copy_to_user(argp, &check, sizeof(check)))
		return -EFAULT;
	return 0;
}

static int argus_resolve_restricted(void __user *argp)
{
	struct argus_policy_restricted_resolution resolution;
	uid_t caller = from_kuid(&init_user_ns, current_euid());

	if (caller != ARGUS_SYSTEM_UID && !capable(CAP_MAC_ADMIN))
		return -EPERM;
	if (copy_from_user(&resolution, argp, sizeof(resolution)))
		return -EFAULT;
	if (resolution.abi_version != ARGUS_POLICY_ABI_VERSION ||
	    resolution.reserved || resolution.approved > 1 ||
	    !resolution.restricted_token)
		return -EINVAL;

	mutex_lock(&argus_restricted_lock);
	argus_expire_restricted_locked();
	if (!argus_restricted.active ||
	    argus_restricted.target_uid != resolution.target_uid ||
	    argus_restricted.token != resolution.restricted_token) {
		mutex_unlock(&argus_restricted_lock);
		return -ESTALE;
	}
	/* Approval controls the captured continuation in system_server.  Both
	 * decisions release the input freeze; queued agent events were never sent. */
	memset(&argus_restricted, 0, sizeof(argus_restricted));
	mutex_unlock(&argus_restricted_lock);
	return 0;
}

static int argus_build_table(const struct argus_policy_replace *replace,
			     struct argus_policy_table **table_out)
{
	struct argus_policy_rule *rules = NULL;
	struct argus_policy_table *table;
	size_t bytes;
	u32 i;
	int ret = 0;

	/* A MAC policy must not silently allow an unenrolled agent operation. */
	if (replace->default_verdict != ARGUS_VERDICT_DENY &&
	    replace->default_verdict != ARGUS_VERDICT_RESTRICTED)
		return -EINVAL;
	if (replace->rule_count > ARGUS_POLICY_MAX_RULES)
		return -E2BIG;
	if (replace->rule_count && !replace->rules_ptr)
		return -EINVAL;
	if (check_mul_overflow((size_t)replace->rule_count,
			       sizeof(*rules), &bytes))
		return -EOVERFLOW;

	if (bytes) {
		rules = memdup_user(u64_to_user_ptr(replace->rules_ptr), bytes);
		if (IS_ERR(rules))
			return PTR_ERR(rules);
	}

	table = argus_table_alloc(replace->default_verdict);
	if (!table) {
		ret = -ENOMEM;
		goto out_rules;
	}

	for (i = 0; i < replace->rule_count; ++i) {
		struct argus_policy_node *node;
		u32 hash;

		if (!argus_verdict_valid(rules[i].verdict) ||
		    !argus_key_valid(&rules[i].key)) {
			ret = -EINVAL;
			goto out_table;
		}
		if (argus_find_node(table, rules[i].target_uid, &rules[i].key)) {
			ret = -EEXIST;
			goto out_table;
		}

		node = kmalloc(sizeof(*node), GFP_KERNEL);
		if (!node) {
			ret = -ENOMEM;
			goto out_table;
		}
		node->rule = rules[i];
		hash = argus_key_hash(node->rule.target_uid, &node->rule.key);
		hash_add(table->rules, &node->hnode, hash);
		table->rule_count++;
	}

	*table_out = table;
	kfree(rules);
	return 0;

out_table:
	argus_table_free(table);
out_rules:
	kfree(rules);
	return ret;
}

static int argus_replace(void __user *argp)
{
	struct argus_policy_replace replace;
	struct argus_policy_table *new_table;
	struct argus_policy_table *old_table;
	int ret;

	if (!capable(CAP_MAC_ADMIN))
		return -EPERM;
	if (copy_from_user(&replace, argp, sizeof(replace)))
		return -EFAULT;
	if (replace.abi_version != ARGUS_POLICY_ABI_VERSION ||
	    (replace.flags & ~ARGUS_REPLACE_F_EXPECT_GENERATION))
		return -EINVAL;

	ret = argus_build_table(&replace, &new_table);
	if (ret)
		return ret;

	mutex_lock(&argus_update_lock);
	old_table = rcu_dereference_protected(argus_active_table,
					       lockdep_is_held(&argus_update_lock));
	if ((replace.flags & ARGUS_REPLACE_F_EXPECT_GENERATION) &&
	    replace.expected_generation != old_table->generation) {
		mutex_unlock(&argus_update_lock);
		argus_table_free(new_table);
		return -ESTALE;
	}
	new_table->generation = old_table->generation + 1;
	rcu_assign_pointer(argus_active_table, new_table);
	mutex_unlock(&argus_update_lock);

	/* Readers finish on the immutable old generation before it is reclaimed. */
	synchronize_rcu();
	argus_table_free(old_table);
	return 0;
}

static int argus_get_info(void __user *argp)
{
	struct argus_policy_info info = {
		.abi_version = ARGUS_POLICY_ABI_VERSION,
	};
	struct argus_policy_table *table;

	rcu_read_lock();
	table = rcu_dereference(argus_active_table);
	if (unlikely(!table)) {
		rcu_read_unlock();
		return -ENODEV;
	}
	info.default_verdict = table->default_verdict;
	info.rule_count = table->rule_count;
	info.generation = table->generation;
	rcu_read_unlock();

	if (copy_to_user(argp, &info, sizeof(info)))
		return -EFAULT;
	return 0;
}

static long argus_policy_ioctl(struct file *file, unsigned int command,
			       unsigned long argument)
{
	void __user *argp = (void __user *)argument;

	switch (command) {
	case ARGUS_POLICY_IOC_QUERY:
		return argus_query(argp);
	case ARGUS_POLICY_IOC_REPLACE:
		return argus_replace(argp);
	case ARGUS_POLICY_IOC_GET_INFO:
		return argus_get_info(argp);
	case ARGUS_POLICY_IOC_INPUT_CHECK:
		return argus_input_check(argp);
	case ARGUS_POLICY_IOC_RESOLVE_RESTRICTED:
		return argus_resolve_restricted(argp);
	default:
		return -ENOTTY;
	}
}

static const struct file_operations argus_policy_fops = {
	.owner = THIS_MODULE,
	.unlocked_ioctl = argus_policy_ioctl,
#ifdef CONFIG_COMPAT
	.compat_ioctl = compat_ptr_ioctl,
#endif
	.llseek = no_llseek,
};

static struct miscdevice argus_policy_device = {
	.minor = MISC_DYNAMIC_MINOR,
	.name = "argus_policy",
	.fops = &argus_policy_fops,
	/* Apps need QUERY access.  Mutating ioctls independently require
	 * CAP_MAC_ADMIN, and Restricted resolution additionally checks UID 1000. */
	.mode = 0666,
};

static int __init argus_policy_init(void)
{
	struct argus_policy_table *initial;
	int ret;

	initial = argus_table_alloc(ARGUS_VERDICT_RESTRICTED);
	if (!initial)
		return -ENOMEM;
	initial->generation = 1;
	rcu_assign_pointer(argus_active_table, initial);

	ret = misc_register(&argus_policy_device);
	if (ret) {
		RCU_INIT_POINTER(argus_active_table, NULL);
		argus_table_free(initial);
		return ret;
	}
	pr_info("Argus policy backend ready (ABI %u, default=restricted)\n",
		ARGUS_POLICY_ABI_VERSION);
	return 0;
}

static void __exit argus_policy_exit(void)
{
	struct argus_policy_table *table;

	misc_deregister(&argus_policy_device);
	mutex_lock(&argus_update_lock);
	table = rcu_dereference_protected(argus_active_table,
					 lockdep_is_held(&argus_update_lock));
	RCU_INIT_POINTER(argus_active_table, NULL);
	mutex_unlock(&argus_update_lock);
	synchronize_rcu();
	argus_table_free(table);
}

module_init(argus_policy_init);
module_exit(argus_policy_exit);

MODULE_DESCRIPTION("Argus DCI+ mandatory GUI-agent policy backend");
MODULE_AUTHOR("Argus authors");
MODULE_LICENSE("GPL");
