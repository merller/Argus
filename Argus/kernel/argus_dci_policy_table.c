/*
 * Core ARGUS addition for kernel/argus_dci_policy_table.c.
 */

#define ARGUS_VERDICT_ALLOW 0
#define ARGUS_VERDICT_BLOCK 1
#define ARGUS_VERDICT_RESTRICTED 2

#define ARGUS_MAX_DOMAIN_BUCKETS 4096
#define ARGUS_MAX_CODEBASE_BUCKETS 1024
#define ARGUS_MAX_INDEX_BUCKETS 512

struct argus_index_node {
    unsigned int index;
    unsigned int agent_source;
    unsigned int verdict;
    struct hlist_node hnode;
};

struct argus_codebase_node {
    char codebase[9];
    struct hlist_head index_buckets[ARGUS_MAX_INDEX_BUCKETS];
    struct hlist_node hnode;
};

struct argus_domain_node {
    char domain[65];
    struct hlist_head codebase_buckets[ARGUS_MAX_CODEBASE_BUCKETS];
    struct hlist_node hnode;
};

static struct hlist_head argus_domain_buckets[ARGUS_MAX_DOMAIN_BUCKETS];
static DEFINE_SPINLOCK(argus_policy_lock);

static u32 argus_hash_string(const char *value, u32 buckets)
{
    u32 hash = 2166136261u;

    while (*value) {
        hash ^= (u8)*value++;
        hash *= 16777619u;
    }
    return hash % buckets;
}

static u32 argus_hash_index(unsigned int index, unsigned int agent_source)
{
    return (index ^ (agent_source * 2654435761u)) % ARGUS_MAX_INDEX_BUCKETS;
}

static struct argus_domain_node *argus_find_domain_locked(const char *domain)
{
    struct argus_domain_node *node;
    u32 bucket = argus_hash_string(domain, ARGUS_MAX_DOMAIN_BUCKETS);

    hlist_for_each_entry(node, &argus_domain_buckets[bucket], hnode) {
        if (!strcmp(node->domain, domain))
            return node;
    }
    return NULL;
}

static struct argus_codebase_node *argus_find_codebase_locked(
        struct argus_domain_node *domain_node, const char *codebase)
{
    struct argus_codebase_node *node;
    u32 bucket = argus_hash_string(codebase, ARGUS_MAX_CODEBASE_BUCKETS);

    hlist_for_each_entry(node, &domain_node->codebase_buckets[bucket], hnode) {
        if (!strcmp(node->codebase, codebase))
            return node;
    }
    return NULL;
}

static struct argus_index_node *argus_find_index_locked(
        struct argus_codebase_node *codebase_node, unsigned int index,
        unsigned int agent_source)
{
    struct argus_index_node *node;
    u32 bucket = argus_hash_index(index, agent_source);

    hlist_for_each_entry(node, &codebase_node->index_buckets[bucket], hnode) {
        if (node->index == index && node->agent_source == agent_source)
            return node;
    }
    return NULL;
}

int argus_lookup_dci_verdict(const char *domain, const char *codebase,
        unsigned int index, unsigned int agent_source, unsigned int *verdict)
{
    struct argus_domain_node *domain_node;
    struct argus_codebase_node *codebase_node;
    struct argus_index_node *index_node;
    unsigned long flags;
    int ret = -ENOENT;

    if (!domain || !codebase || !verdict)
        return -EINVAL;

    spin_lock_irqsave(&argus_policy_lock, flags);

    domain_node = argus_find_domain_locked(domain);
    if (!domain_node)
        goto out;

    codebase_node = argus_find_codebase_locked(domain_node, codebase);
    if (!codebase_node)
        goto out;

    index_node = argus_find_index_locked(codebase_node, index, agent_source);
    if (!index_node)
        goto out;

    *verdict = index_node->verdict;
    ret = 0;

out:
    spin_unlock_irqrestore(&argus_policy_lock, flags);
    return ret;
}
