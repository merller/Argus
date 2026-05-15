/*
 * Core ARGUS addition for frameworks/base/core/jni/ArgusPolicyTable.cpp.
 */

class ArgusPolicyTable {
public:
    static ArgusPolicyTable& GetInstance() {
        static ArgusPolicyTable table;
        return table;
    }

    int Lookup(const char* domain, const char* codebase, int index, int agent_source,
            int default_verdict) {
        if (domain == nullptr || codebase == nullptr || index < 0) {
            return default_verdict;
        }

        ArgusDciLookupRequest request = {
                .domain = domain,
                .codebase = codebase,
                .index = index,
                .agent_source = agent_source,
        };
        ArgusDciLookupResponse response = {
                .verdict = default_verdict,
        };

        if (!LookupInKernelDciTable(request, &response)) {
            return default_verdict;
        }
        return response.verdict;
    }

private:
    struct ArgusDciLookupRequest {
        const char* domain;
        const char* codebase;
        int index;
        int agent_source;
    };

    struct ArgusDciLookupResponse {
        int verdict;
    };

    ArgusPolicyTable() = default;

    bool LookupInKernelDciTable(const ArgusDciLookupRequest& request,
            ArgusDciLookupResponse* response) {
        /*
         * The production build uses the kernel-backed decision channel. The
         * kernel table is indexed as Domain -> Codebase -> Index, so each
         * stage narrows the namespace before touching method-index entries.
         */
        return argus_dci_lookup(
                request.domain,
                request.codebase,
                request.index,
                request.agent_source,
                &response->verdict) == 0;
    }

    int argus_dci_lookup(const char* domain, const char* codebase, int index,
            int agent_source, int* verdict_out) {
        /*
         * Replace this shim with the concrete syscall/ioctl/procfs entry used
         * in the experiment. The corresponding kernel-side core table is shown
         * in kernel/argus_dci_policy_table.c.
         */
        return -1;
    }
};
