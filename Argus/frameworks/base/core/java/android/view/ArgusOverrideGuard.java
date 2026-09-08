/*
 * Core ARGUS addition for frameworks/base/core/java/android/view/ArgusOverrideGuard.java.
 */

package android.view;

import java.lang.reflect.Method;

/**
 * Detects app classes that override framework entry points and skip super.
 *
 * @hide
 */
final class ArgusOverrideGuard {
    private ArgusOverrideGuard() {}

    static boolean hasRequiredSuperDispatch(View target) {
        return methodCallsFrameworkSuper(target.getClass(), "performClick")
                && methodCallsFrameworkSuper(target.getClass(), "performAccessibilityAction",
                        int.class, android.os.Bundle.class);
    }

    private static boolean methodCallsFrameworkSuper(Class<?> clazz, String name,
            Class<?>... parameterTypes) {
        final Method method;
        try {
            method = clazz.getMethod(name, parameterTypes);
        } catch (NoSuchMethodException e) {
            return true;
        }
        if (method.getDeclaringClass() == View.class) {
            return true;
        }
        return nativeMethodCallsSuper(method, View.class.getName(), name);
    }

    private static native boolean nativeMethodCallsSuper(Method method, String superClassName,
            String methodName);
}
