/*
 * Core ARGUS addition for frameworks/base/core/java/android/view/ArgusClickDci.java.
 */

package android.view;

/**
 * Domain-Codebase-Index identity of the callback about to be invoked.
 *
 * @hide
 */
final class ArgusClickDci {
    final String domain;
    final String codebase;
    final int index;
    final int viewId;
    final String packageName;
    final String listenerClass;
    final String handlerMethod;
    final boolean blockedByOverride;

    ArgusClickDci(String domain, String codebase, int index, int viewId, String packageName,
            String listenerClass, String handlerMethod) {
        this.domain = domain;
        this.codebase = codebase;
        this.index = index;
        this.viewId = viewId;
        this.packageName = packageName;
        this.listenerClass = listenerClass;
        this.handlerMethod = handlerMethod;
        this.blockedByOverride = false;
    }

    private ArgusClickDci(View target) {
        this.domain = "override-bypass";
        this.codebase = "override-bypass";
        this.index = -1;
        this.viewId = target.getId();
        this.packageName = target.getContext() != null ? target.getContext().getPackageName() : "";
        this.listenerClass = target.getClass().getName();
        this.handlerMethod = "missing-super";
        this.blockedByOverride = true;
    }

    static ArgusClickDci blockedByOverride(View target) {
        return new ArgusClickDci(target);
    }

    boolean isBlockedByOverride() {
        return blockedByOverride;
    }
}
