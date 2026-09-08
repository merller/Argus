package android.view;

import android.content.Context;
import android.content.DialogInterface;
import android.content.pm.PackageInfo;
import android.content.pm.PackageManager;
import android.content.pm.Signature;
import android.content.pm.SigningInfo;
import android.os.Build;
import android.os.Debug;
import android.os.Message;
import android.os.Process;
import android.os.SystemProperties;
import android.util.Log;
import android.widget.AdapterView;
import android.widget.Button;

import java.lang.reflect.Field;
import java.lang.reflect.Modifier;
import java.lang.ref.WeakReference;
import java.security.MessageDigest;
import java.security.NoSuchAlgorithmException;
import java.util.ArrayList;
import java.util.Comparator;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.atomic.AtomicBoolean;

/**
 * Framework-side prototype resolver for Argus DCI-plus.
 *
 * <p>The generic route below is deliberately bounded and never invokes an app getter or
 * {@code toString()}. Production enforcement replaces this heuristic route with a signed,
 * per-build Argus Binding Manifest recipe executed by ART.</p>
 *
 * @hide
 */
public final class ArgusDci {
    public static final int INPUT_SOURCE_UNKNOWN = 0;
    public static final int INPUT_SOURCE_PHYSICAL = 1;
    public static final int INPUT_SOURCE_INJECTED = 2;
    public static final int INPUT_SOURCE_ACCESSIBILITY = 3;
    public static final int INPUT_SOURCE_IME = 4;

    static final int VERDICT_UNRESOLVED = -1;
    static final int VERDICT_ALLOWED = 0;
    static final int VERDICT_DENIED = 1;
    static final int VERDICT_RESTRICTED = 2;

    private static final String TAG = "ArgusDci";
    private static final int MAX_PARENT_DEPTH = 12;
    private static final int MAX_FIELDS_PER_OBJECT = 12;
    private static final int MAX_ROUTE_CHARS = 4096;
    private static final String EXPERIMENTAL_ENFORCE_PROPERTY =
            "debug.argus.experimental_enforce";
    private static final String EXPERIMENTAL_PACKAGE_PROPERTY =
            "debug.argus.experimental_package";
    private static final String EXPERIMENTAL_DENY_DCIPLUS_PROPERTY =
            "debug.argus.experimental_deny_dciplus";
    private static final String EXPERIMENTAL_POLICY_DCIPLUS_PROPERTY =
            "debug.argus.experimental_policy_dciplus";
    private static final String CERTIFIED_DIALOG_SELECTOR_PROPERTY =
            "debug.argus.certified_dialog_selector";
    private static final String CERTIFIED_DIALOG_ROUTE_PREFIX =
            "ARGUS/CERTIFIED-DIALOG/v1|";
    private static final String CERTIFIED_SELF_ROUTE_SELECTOR_PROPERTY =
            "debug.argus.self_route";
    private static final String CERTIFIED_SELF_ROUTE_PREFIX =
            "ARGUS/CERTIFIED-SELF-ROUTE/v1|";
    private static final String DISPATCH_PATH_SELECTOR_PROPERTY =
            "debug.argus.dispatch_path_selector";
    private static final String DISPATCH_PATH_ROUTE =
            "ARGUS/DISPATCH-PATH/v1";

    private static final ConcurrentHashMap<String, String> sDomainCache =
            new ConcurrentHashMap<>();
    private static final AtomicBoolean sNativeFailureLogged = new AtomicBoolean();
    private static final ThreadLocal<Long> sCertifiedDialogContinuation = new ThreadLocal<>();
    private static final Object sClickBindingLock = new Object();
    private static final ArrayList<ClickBindingGroup> sClickBindingGroups = new ArrayList<>();
    private static volatile boolean sNativeAvailable = true;

    /**
     * Weak, identity-only registry used solely to certify that one live callback is bound to
     * multiple Views.  Neither the View references nor any View attribute becomes identity
     * material.  Avoiding Map here is intentional: an application callback may override
     * hashCode()/equals(), and framework identity measurement must never invoke either method.
     */
    private static final class ClickBindingGroup {
        final WeakReference<Object> callback;
        final ArrayList<WeakReference<View>> targets = new ArrayList<>();

        ClickBindingGroup(Object callback) {
            this.callback = new WeakReference<>(callback);
        }
    }

    private static final class CertifiedBinding {
        final Object callback;
        final String methodName;
        final String descriptor;
        final int dialogButtonRole;

        CertifiedBinding(Object callback, String methodName, String descriptor,
                int dialogButtonRole) {
            this.callback = callback;
            this.methodName = methodName;
            this.descriptor = descriptor;
            this.dialogButtonRole = dialogButtonRole;
        }
    }

    private static final ThreadLocal<MessageDigest> sSha256 =
            ThreadLocal.withInitial(() -> {
                try {
                    return MessageDigest.getInstance("SHA-256");
                } catch (NoSuchAlgorithmException e) {
                    throw new AssertionError(e);
                }
            });

    private static final ClassValue<Field[]> sCaptureFields = new ClassValue<Field[]>() {
        @Override
        protected Field[] computeValue(Class<?> type) {
            final ArrayList<Field> result = new ArrayList<>();
            Class<?> current = type;
            int hierarchyDepth = 0;
            while (current != null && current != Object.class && hierarchyDepth++ < 3
                    && result.size() < MAX_FIELDS_PER_OBJECT) {
                final Field[] fields;
                try {
                    fields = current.getDeclaredFields();
                } catch (Throwable ignored) {
                    break;
                }
                for (Field field : fields) {
                    if (result.size() >= MAX_FIELDS_PER_OBJECT) {
                        break;
                    }
                    // A generic prototype must never turn mutable app state into an object ID.
                    // Production uses only the exact fields named by a signed ABM recipe.
                    if (Modifier.isStatic(field.getModifiers())
                            || !Modifier.isFinal(field.getModifiers())) {
                        continue;
                    }
                    try {
                        field.setAccessible(true);
                        result.add(field);
                    } catch (Throwable ignored) {
                        // Hidden or inaccessible fields are conservatively omitted.
                    }
                }
                current = current.getSuperclass();
            }
            result.sort(Comparator.comparing(
                    field -> field.getDeclaringClass().getName() + "#" + field.getName()));
            return result.toArray(new Field[0]);
        }
    };

    private static final ClassValue<Boolean> sUsesFrameworkPerformClick =
            new ClassValue<Boolean>() {
                @Override
                protected Boolean computeValue(Class<?> type) {
                    try {
                        return type.getMethod("performClick").getDeclaringClass() == View.class;
                    } catch (Throwable ignored) {
                        return false;
                    }
                }
            };

    private static final Field sViewGroupChildren;
    private static final Field sViewGroupChildrenCount;
    private static final Field sAdapterFirstPosition;

    static {
        sViewGroupChildren = resolveFrameworkField(ViewGroup.class, "mChildren");
        sViewGroupChildrenCount = resolveFrameworkField(ViewGroup.class, "mChildrenCount");
        sAdapterFirstPosition = resolveFrameworkField(AdapterView.class, "mFirstPosition");
    }

    private ArgusDci() {
    }

    static void noteClickBinding(View target, Object previousCallback, Object newCallback) {
        if (target == null) {
            return;
        }
        synchronized (sClickBindingLock) {
            ClickBindingGroup destination = null;
            for (int groupIndex = sClickBindingGroups.size() - 1; groupIndex >= 0; groupIndex--) {
                final ClickBindingGroup group = sClickBindingGroups.get(groupIndex);
                final Object callback = group.callback.get();
                for (int targetIndex = group.targets.size() - 1; targetIndex >= 0; targetIndex--) {
                    final View existing = group.targets.get(targetIndex).get();
                    if (existing == null || existing == target) {
                        group.targets.remove(targetIndex);
                    }
                }
                if (callback == null) {
                    sClickBindingGroups.remove(groupIndex);
                } else if (callback == newCallback) {
                    destination = group;
                }
            }
            if (newCallback == null) {
                return;
            }
            if (destination == null) {
                destination = new ClickBindingGroup(newCallback);
                sClickBindingGroups.add(destination);
            }
            destination.targets.add(new WeakReference<>(target));
        }
    }

    private static boolean isSharedClickBinding(Object callback) {
        synchronized (sClickBindingLock) {
            for (int groupIndex = sClickBindingGroups.size() - 1; groupIndex >= 0; groupIndex--) {
                final ClickBindingGroup group = sClickBindingGroups.get(groupIndex);
                final Object existingCallback = group.callback.get();
                int liveTargets = 0;
                for (int targetIndex = group.targets.size() - 1; targetIndex >= 0; targetIndex--) {
                    if (group.targets.get(targetIndex).get() == null) {
                        group.targets.remove(targetIndex);
                    } else {
                        liveTargets++;
                    }
                }
                if (existingCallback == null) {
                    sClickBindingGroups.remove(groupIndex);
                } else if (existingCallback == callback) {
                    return liveTargets > 1;
                }
            }
            return false;
        }
    }

    public static boolean log(View target, Object callback, String methodName,
            String descriptor, String operation, String phase, int inputEventId,
            int inputSource, String arguments) {
        return resolvePolicy(target, callback, methodName, descriptor, operation, phase,
                inputEventId, inputSource, arguments) == VERDICT_ALLOWED;
    }

    static int resolvePolicy(View target, Object callback, String methodName,
            String descriptor, String operation, String phase, int inputEventId,
            int inputSource, String arguments) {
        if (inputSource == INPUT_SOURCE_PHYSICAL) {
            return VERDICT_ALLOWED;
        }
        Context context = target != null ? target.mContext : null;
        String domain = "unknown";
        int nativeVerdict = VERDICT_UNRESOLVED;
        int targetId = target != null ? target.mID : View.NO_ID;
        try {
            if (target == null || callback == null) {
                return applyExperimentalVerdict(context, domain, inputSource, operation,
                        targetId, VERDICT_UNRESOLVED);
            }
            final String routeDigest = buildViewRouteDigest(target, callback);
            domain = domainFor(context);
            nativeVerdict = callNative(callback, methodName, descriptor, domain,
                    targetId, bindingToken(target, callback), inputEventId, inputSource,
                    operation, phase, routeDigest, digestUtf8Bounded(arguments));
        } catch (Throwable error) {
            Log.w(TAG, "Argus view measurement failed closed as unresolved", error);
        }
        return applyExperimentalVerdict(context, domain, inputSource, operation, targetId,
                nativeVerdict);
    }

    public static boolean log(Context context, int targetId, Object callback, String methodName,
            String descriptor, String operation, String phase, int inputEventId,
            int inputSource, String arguments) {
        return resolvePolicy(context, targetId, callback, methodName, descriptor, operation,
                phase, inputEventId, inputSource, arguments) == VERDICT_ALLOWED;
    }

    static int resolvePolicy(Context context, int targetId, Object callback, String methodName,
            String descriptor, String operation, String phase, int inputEventId,
            int inputSource, String arguments) {
        if (inputSource == INPUT_SOURCE_PHYSICAL) {
            return VERDICT_ALLOWED;
        }
        String domain = "unknown";
        int nativeVerdict = VERDICT_UNRESOLVED;
        try {
            if (context == null || callback == null) {
                return applyExperimentalVerdict(context, domain, inputSource, operation,
                        targetId, VERDICT_UNRESOLVED);
            }
            final String canonical = "targetId=" + targetId
                    + ";callback=" + callback.getClass().getName()
                    + ";certifiedSelectorPrototype=" + captureDigest(callback);
            final String routeDigest = digestUtf8(canonical);
            domain = domainFor(context);
            nativeVerdict = callNative(callback, methodName, descriptor, domain, targetId,
                    firstLong(routeDigest), inputEventId, inputSource, operation, phase,
                    routeDigest, digestUtf8Bounded(arguments));
        } catch (Throwable error) {
            Log.w(TAG, "Argus context measurement failed closed as unresolved", error);
        }
        return applyExperimentalVerdict(context, domain, inputSource, operation, targetId,
                nativeVerdict);
    }

    /**
     * Applies a deliberately narrow, userdebug-only three-valued policy decision.
     *
     * <p>The native return value remains observational everywhere except an explicitly selected
     * application receiving injected input with all three debug properties set. Production
     * enforcement replaces these properties with a system-owned operation token and signed
     * ABM policy record.</p>
     */
    private static int applyExperimentalVerdict(Context context, String domain,
            int inputSource, String operation, int targetId, int nativeVerdict) {
        final String scopedPackage = SystemProperties.get(EXPERIMENTAL_PACKAGE_PROPERTY, "");
        final String policyDciPlus = configuredPolicyDciPlus();
        final boolean inScope = Build.IS_DEBUGGABLE
                && SystemProperties.getBoolean(EXPERIMENTAL_ENFORCE_PROPERTY, false)
                && isAgentInputSource(inputSource)
                && Process.myUid() >= Process.FIRST_APPLICATION_UID
                && !scopedPackage.isEmpty()
                && context != null
                && scopedPackage.equals(context.getPackageName())
                && isLowerHexSha256(policyDciPlus);
        if (!inScope) {
            return VERDICT_ALLOWED;
        }

        // A signer/domain resolution failure is fail-closed only inside the explicit scope.
        final boolean domainMatches = domain != null
                && domain.startsWith(scopedPackage + ':');
        final int verdict = domainMatches && isValidVerdict(nativeVerdict)
                ? nativeVerdict : VERDICT_DENIED;
        Log.i(TAG, "Argus experimental enforcement package=" + scopedPackage
                + " operation=" + operation + " targetId=" + targetId
                + " source=" + inputSourceName(inputSource)
                + " decision=" + verdictName(verdict)
                + " reason=" + (!domainMatches ? "domain_unresolved_or_mismatch"
                        : isValidVerdict(nativeVerdict) ? "policy_lookup"
                                : "dciplus_resolver_failure"));
        return verdict;
    }

    private static String configuredPolicyDciPlus() {
        final String configured = SystemProperties.get(
                EXPERIMENTAL_POLICY_DCIPLUS_PROPERTY, "");
        if (!configured.isEmpty()) {
            return configured;
        }
        // Compatibility with the earlier two-valued deny experiment.
        return SystemProperties.get(EXPERIMENTAL_DENY_DCIPLUS_PROPERTY, "");
    }

    static boolean isAgentInputSource(int inputSource) {
        return inputSource == INPUT_SOURCE_INJECTED
                || inputSource == INPUT_SOURCE_ACCESSIBILITY;
    }

    private static boolean isValidVerdict(int verdict) {
        return verdict == VERDICT_ALLOWED || verdict == VERDICT_DENIED
                || verdict == VERDICT_RESTRICTED;
    }

    private static String verdictName(int verdict) {
        switch (verdict) {
            case VERDICT_ALLOWED:
                return "allowed";
            case VERDICT_RESTRICTED:
                return "restricted";
            default:
                return "denied";
        }
    }

    private static String inputSourceName(int inputSource) {
        switch (inputSource) {
            case INPUT_SOURCE_INJECTED:
                return "injected";
            case INPUT_SOURCE_ACCESSIBILITY:
                return "accessibility";
            case INPUT_SOURCE_PHYSICAL:
                return "physical";
            default:
                return "unknown";
        }
    }

    private static boolean isLowerHexSha256(String value) {
        if (value == null || value.length() != 64) {
            return false;
        }
        for (int index = 0; index < value.length(); index++) {
            final char character = value.charAt(index);
            if (!((character >= '0' && character <= '9')
                    || (character >= 'a' && character <= 'f'))) {
                return false;
            }
        }
        return true;
    }

    public static int currentInputSource() {
        return InputEventReceiver.getCurrentInputEventSource();
    }

    public static int currentInputEventId() {
        return InputEventReceiver.getCurrentInputEventId();
    }

    public static long pushInputContextForAsync(int inputSource, int inputEventId) {
        return InputEventReceiver.pushInputEventContext(inputSource, inputEventId);
    }

    public static long pushInputContextForAsync(int inputSource, int inputEventId,
            long subjectCounter, long subjectMac) {
        return InputEventReceiver.pushInputEventContext(
                inputSource, inputEventId, subjectCounter, subjectMac);
    }

    public static void popInputContextForAsync(long previousContext) {
        InputEventReceiver.popInputEventContext(previousContext);
    }

    /**
     * Arms ART immediately before the exact callback object is invoked.
     *
     * <p>A positive token denotes an observational/enforcing ART scope, zero means that the
     * operation is out of scope, and a negative value is a fail-closed decision.  The same local
     * callback reference supplied here must be the one invoked by the caller.</p>
     *
     * @hide
     */
    public static long beginContinuation(View target, Object callback, String methodName,
            String descriptor, String operation, int inputEventId, int inputSource,
            String arguments) {
        return beginContinuationWithBindingCallback(target, callback, methodName, descriptor,
                operation, inputEventId, inputSource, callback, arguments);
    }

    /**
     * Arms an exact virtual-dispatch root while binding the policy identity to the business
     * callback selected by the framework.
     *
     * <p>This split is needed for non-cooperative subclasses: the trusted framework can arm
     * {@code View.performClick()} (including an override that never calls {@code super}) while
     * retaining the listener-derived DCI+ route as the object identity.  Neither reference is
     * supplied by the application as metadata; both are live objects resolved by ART.</p>
     *
     * @hide
     */
    public static long beginContinuationWithBindingCallback(View target, Object rootCallback,
            String methodName, String descriptor, String operation, int inputEventId,
            int inputSource, Object bindingCallback, String arguments) {
        if (inputSource != INPUT_SOURCE_INJECTED
                && inputSource != INPUT_SOURCE_ACCESSIBILITY) {
            return 0;
        }
        if (target == null || rootCallback == null || bindingCallback == null) {
            return -1;
        }
        try {
            final String domain = domainFor(target.mContext);
            Object effectiveBindingCallback = bindingCallback;
            String bindingMethodName;
            String bindingDescriptor;
            if (bindingCallback == rootCallback) {
                bindingMethodName = methodName;
                bindingDescriptor = descriptor;
            } else if ("click".equals(operation)) {
                bindingMethodName = "onClick";
                bindingDescriptor = "(Landroid/view/View;)V";
            } else if ("long_click".equals(operation)) {
                bindingMethodName = "onLongClick";
                bindingDescriptor = "(Landroid/view/View;)Z";
            } else {
                // A new split-root operation must define its trusted callback interface here.
                return -1;
            }

            CertifiedBinding certifiedBinding = null;
            long certifiedWallNs = 0L;
            long certifiedCpuNs = 0L;
            final boolean certifiedSelectorAttempted = "click".equals(operation)
                    && SystemProperties.getBoolean(CERTIFIED_DIALOG_SELECTOR_PROPERTY, false)
                    && sCertifiedDialogContinuation.get() == null;
            if (certifiedSelectorAttempted) {
                final long wallStartNs = System.nanoTime();
                final long cpuStartNs = Debug.threadCpuTimeNanos();
                certifiedBinding = resolveCertifiedDialogBinding(target, bindingCallback);
                certifiedCpuNs = Debug.threadCpuTimeNanos() - cpuStartNs;
                certifiedWallNs = System.nanoTime() - wallStartNs;
            }
            if (certifiedBinding != null) {
                effectiveBindingCallback = certifiedBinding.callback;
                bindingMethodName = certifiedBinding.methodName;
                bindingDescriptor = certifiedBinding.descriptor;
            }
            final boolean dispatchPathSelector = certifiedBinding == null
                    && "click".equals(operation)
                    && bindingCallback != rootCallback
                    && isSharedClickBinding(bindingCallback)
                    && SystemProperties.getBoolean(DISPATCH_PATH_SELECTOR_PROPERTY, false);
            // A listener-less View can dispatch its click entirely inside an overridden
            // performClick(), leaving no outbound listener edge for the generic selector.
            // Its concrete View route is nevertheless measured at this trusted framework
            // boundary.  Certify only this narrowly provable self-dispatch shape; ordinary
            // listener callbacks continue to use the deeper APK-local call selector.
            final boolean certifiedSelfRoute = certifiedBinding == null
                    && "click".equals(operation)
                    && "performClick".equals(methodName)
                    && "()Z".equals(descriptor)
                    && target == rootCallback
                    && bindingCallback == rootCallback
                    && sUsesFrameworkPerformClick.get(target.getClass())
                    && SystemProperties.getBoolean(
                            CERTIFIED_SELF_ROUTE_SELECTOR_PROPERTY, false);
            final String routeDigest;
            if (certifiedBinding != null) {
                final String measuredRouteDigest =
                        buildViewRouteDigest(target, effectiveBindingCallback);
                routeDigest = CERTIFIED_DIALOG_ROUTE_PREFIX
                        + certifiedBinding.dialogButtonRole + '|' + measuredRouteDigest;
            } else if (dispatchPathSelector) {
                // This recipe contains no target metadata. ART derives the selector exclusively
                // from executed Dex branch and call sites inside the shared callback.
                routeDigest = DISPATCH_PATH_ROUTE;
            } else if (certifiedSelfRoute) {
                final String measuredRouteDigest =
                        buildViewRouteDigest(target, effectiveBindingCallback);
                routeDigest = CERTIFIED_SELF_ROUTE_PREFIX + measuredRouteDigest;
            } else {
                routeDigest = buildViewRouteDigest(target, effectiveBindingCallback);
            }
            final long token = nativeBeginContinuation(rootCallback, methodName, descriptor,
                    effectiveBindingCallback, bindingMethodName, bindingDescriptor, domain,
                    inputEventId, inputSource, operation, routeDigest,
                    digestUtf8Bounded(arguments));
            if (certifiedSelectorAttempted && token > 0) {
                // A View click has an outer performClick continuation and a nested listener
                // continuation. ART folds the latter; remember the live outer token so the
                // nested framework hook does not repeat this reflection recipe unnecessarily.
                sCertifiedDialogContinuation.set(token);
            }
            if (certifiedSelectorAttempted) {
                Log.i(TAG, "ArgusCertifiedSelectorAttempt"
                        + " recipe=dialog_message_delegate_v1"
                        + " eventId=" + inputEventId
                        + " resolved=" + (certifiedBinding != null ? "yes" : "no")
                        + " buttonRole="
                        + (certifiedBinding != null ? certifiedBinding.dialogButtonRole : 0)
                        + " wrapper=" + bindingCallback.getClass().getName()
                        + " delegate=" + effectiveBindingCallback.getClass().getName()
                        + " resolverCpuNs=" + certifiedCpuNs
                        + " resolverElapsedNs=" + certifiedWallNs);
            }
            return token;
        } catch (Throwable error) {
            Log.w(TAG, "Argus continuation arm failed closed", error);
            return -1;
        }
    }

    /** @hide */
    public static int endContinuation(long token) {
        if (token <= 0) {
            return 0;
        }
        try {
            return nativeEndContinuation(token);
        } catch (Throwable error) {
            Log.w(TAG, "Argus continuation end failed", error);
            return -1;
        } finally {
            final Long activeToken = sCertifiedDialogContinuation.get();
            if (activeToken != null && activeToken.longValue() == token) {
                sCertifiedDialogContinuation.remove();
            }
        }
    }

    private static int callNative(Object callback, String methodName, String descriptor,
            String domain, int targetId, long targetToken, int inputEventId, int inputSource,
            String operation, String phase, String route, String argumentsHash) {
        if (!sNativeAvailable) {
            return VERDICT_UNRESOLVED;
        }
        try {
            return nativeResolveAndLog(callback, methodName, descriptor, domain, targetId,
                    targetToken, inputEventId, inputSource, operation, phase, route,
                    argumentsHash);
        } catch (UnsatisfiedLinkError error) {
            sNativeAvailable = false;
            if (sNativeFailureLogged.compareAndSet(false, true)) {
                Log.e(TAG, "Argus native resolver unavailable; operation is unresolved", error);
            }
            return VERDICT_UNRESOLVED;
        } catch (RuntimeException error) {
            Log.w(TAG, "Argus DCI-plus resolution failed", error);
            return VERDICT_UNRESOLVED;
        }
    }

    private static String buildViewRouteDigest(View target, Object callback) {
        final StringBuilder route = new StringBuilder(512);
        route.append("targetClass=").append(target.getClass().getName())
                .append(";targetId=").append(target.mID);

        View current = target;
        for (int depth = 0; depth < MAX_PARENT_DEPTH && current != null; depth++) {
            final ViewParent parent = current.mParent;
            if (!(parent instanceof View)) {
                break;
            }
            final View parentView = (View) parent;
            route.append(";p").append(depth).append('=')
                    .append(parentView.getClass().getName()).append(':').append(parentView.mID);
            if (parentView instanceof AdapterView) {
                route.append(":adapterPosition=")
                        .append(directAdapterPosition((AdapterView<?>) parentView, current));
            } else {
                route.append(":child=").append(directChildIndex(parentView, current));
            }
            if (route.length() >= MAX_ROUTE_CHARS) {
                break;
            }
            current = parentView;
        }
        route.append(";callback=").append(callback.getClass().getName())
                .append(";certifiedSelectorPrototype=").append(captureDigest(callback));
        if (route.length() > MAX_ROUTE_CHARS) {
            route.setLength(MAX_ROUTE_CHARS);
        }
        return digestUtf8(route.toString());
    }

    /**
     * Measurement-only prototype for the signed ABM dialog recipe.  The live click target must
     * be one of an AlertController's three bound buttons, and the matching Message must carry a
     * real DialogInterface.OnClickListener.  No application getter or toString() is invoked.
     */
    private static CertifiedBinding resolveCertifiedDialogBinding(
            View target, Object wrapperCallback) {
        final int buttonRole = dialogButtonRole(target.getId());
        if (buttonRole == 0 || wrapperCallback == null) {
            return null;
        }
        Class<?> callbackClass = wrapperCallback.getClass();
        int callbackDepth = 0;
        while (callbackClass != null && callbackClass != Object.class
                && callbackDepth++ < 3) {
            final Field[] fields;
            try {
                fields = callbackClass.getDeclaredFields();
            } catch (Throwable ignored) {
                return null;
            }
            int inspected = 0;
            for (Field field : fields) {
                if (inspected++ >= 24 || Modifier.isStatic(field.getModifiers())) {
                    continue;
                }
                final Object controller;
                try {
                    field.setAccessible(true);
                    controller = field.get(wrapperCallback);
                } catch (Throwable ignored) {
                    continue;
                }
                if (controller == null
                        || !controller.getClass().getName().endsWith(".AlertController")) {
                    continue;
                }
                final Object delegate = dialogDelegate(controller, target, buttonRole);
                if (delegate instanceof DialogInterface.OnClickListener
                        && delegate != wrapperCallback) {
                    return new CertifiedBinding(delegate, "onClick",
                            "(Landroid/content/DialogInterface;I)V", buttonRole);
                }
            }
            callbackClass = callbackClass.getSuperclass();
        }
        return null;
    }

    private static Object dialogDelegate(Object controller, View target, int buttonRole) {
        boolean targetIsBoundButton = false;
        Object delegate = null;
        Class<?> controllerClass = controller.getClass();
        int hierarchyDepth = 0;
        int inspected = 0;
        while (controllerClass != null && controllerClass != Object.class
                && hierarchyDepth++ < 3 && inspected < 128) {
            final Field[] fields;
            try {
                fields = controllerClass.getDeclaredFields();
            } catch (Throwable ignored) {
                return null;
            }
            for (Field field : fields) {
                if (inspected++ >= 128 || Modifier.isStatic(field.getModifiers())) {
                    continue;
                }
                final Object value;
                try {
                    field.setAccessible(true);
                    value = field.get(controller);
                } catch (Throwable ignored) {
                    continue;
                }
                if (value == target && value instanceof Button) {
                    targetIsBoundButton = true;
                } else if (value instanceof Message) {
                    final Message message = (Message) value;
                    if (message.what == buttonRole
                            && message.obj instanceof DialogInterface.OnClickListener) {
                        if (delegate != null && delegate != message.obj) {
                            return null;
                        }
                        delegate = message.obj;
                    }
                }
            }
            controllerClass = controllerClass.getSuperclass();
        }
        return targetIsBoundButton ? delegate : null;
    }

    private static int dialogButtonRole(int viewId) {
        if (viewId == android.R.id.button1) {
            return DialogInterface.BUTTON_POSITIVE;
        } else if (viewId == android.R.id.button2) {
            return DialogInterface.BUTTON_NEGATIVE;
        } else if (viewId == android.R.id.button3) {
            return DialogInterface.BUTTON_NEUTRAL;
        }
        return 0;
    }

    private static int directChildIndex(View parent, View child) {
        if (!(parent instanceof ViewGroup) || sViewGroupChildren == null
                || sViewGroupChildrenCount == null) {
            return -1;
        }
        try {
            final View[] children = (View[]) sViewGroupChildren.get(parent);
            final int count = sViewGroupChildrenCount.getInt(parent);
            for (int index = 0; children != null && index < count; index++) {
                if (children[index] == child) {
                    return index;
                }
            }
        } catch (Throwable ignored) {
        }
        return -1;
    }

    private static int directAdapterPosition(AdapterView<?> parent, View descendant) {
        if (sAdapterFirstPosition == null) {
            return AdapterView.INVALID_POSITION;
        }
        View item = descendant;
        while (item != null && item.mParent instanceof View && item.mParent != parent) {
            item = (View) item.mParent;
        }
        final int childIndex = item != null ? directChildIndex(parent, item) : -1;
        if (childIndex < 0) {
            return AdapterView.INVALID_POSITION;
        }
        try {
            return sAdapterFirstPosition.getInt(parent) + childIndex;
        } catch (Throwable ignored) {
            return AdapterView.INVALID_POSITION;
        }
    }

    private static String captureDigest(Object callback) {
        final StringBuilder captures = new StringBuilder(512);
        appendObjectFields(captures, callback, 0);
        if (captures.length() > MAX_ROUTE_CHARS) {
            captures.setLength(MAX_ROUTE_CHARS);
        }
        return digestUtf8(captures.toString());
    }

    private static void appendObjectFields(StringBuilder output, Object object, int depth) {
        if (object == null || output.length() >= MAX_ROUTE_CHARS) {
            return;
        }
        final Field[] fields = sCaptureFields.get(object.getClass());
        int count = 0;
        for (Field field : fields) {
            if (count++ >= MAX_FIELDS_PER_OBJECT || output.length() >= MAX_ROUTE_CHARS) {
                break;
            }
            output.append(field.getDeclaringClass().getName()).append('#')
                    .append(field.getName()).append('=');
            try {
                final Object value = field.get(object);
                appendLeafValue(output, value);
                if (depth == 0 && value != null && !isLeaf(value)
                        && !(value instanceof Context) && !(value instanceof View)) {
                    output.append('{');
                    appendObjectFields(output, value, 1);
                    output.append('}');
                }
            } catch (Throwable ignored) {
                output.append("<unreadable>");
            }
            output.append(';');
        }
    }

    private static void appendLeafValue(StringBuilder output, Object value) {
        if (value == null) {
            output.append("null");
        } else if (value instanceof String) {
            final String string = (String) value;
            output.append("String:").append(string.length()).append(':')
                    .append(digestUtf8Bounded(string));
        } else if (value instanceof Number || value instanceof Boolean
                || value instanceof Character) {
            output.append(value.getClass().getName()).append(':').append(value);
        } else if (value instanceof Enum) {
            output.append(value.getClass().getName()).append(':').append(((Enum<?>) value).name());
        } else if (value instanceof View) {
            final View view = (View) value;
            output.append(view.getClass().getName()).append(':').append(view.mID);
        } else {
            output.append(value.getClass().getName());
        }
    }

    private static boolean isLeaf(Object value) {
        return value instanceof String || value instanceof Number || value instanceof Boolean
                || value instanceof Character || value instanceof Enum;
    }

    private static Field resolveFrameworkField(Class<?> owner, String name) {
        try {
            final Field field = owner.getDeclaredField(name);
            field.setAccessible(true);
            return field;
        } catch (Throwable ignored) {
            return null;
        }
    }

    private static String domainFor(Context context) {
        if (context == null) {
            return "unknown";
        }
        final String packageName = context.getPackageName();
        final String cached = sDomainCache.get(packageName);
        if (cached != null) {
            return cached;
        }
        final String measured = computeDomain(context, packageName);
        final String previous = sDomainCache.putIfAbsent(packageName, measured);
        return previous != null ? previous : measured;
    }

    private static String computeDomain(Context context, String packageName) {
        try {
            final PackageInfo packageInfo = context.getPackageManager().getPackageInfo(packageName,
                    PackageManager.GET_SIGNING_CERTIFICATES);
            final SigningInfo signingInfo = packageInfo.signingInfo;
            if (signingInfo == null) {
                return "unknown";
            }
            Signature[] signatures = signingInfo.getApkContentsSigners();
            if ((signatures == null || signatures.length == 0)
                    && signingInfo.getSigningCertificateHistory() != null) {
                signatures = signingInfo.getSigningCertificateHistory();
            }
            if (signatures == null || signatures.length == 0) {
                return "unknown";
            }
            return packageName + ':' + digest(signatures[0].toByteArray());
        } catch (PackageManager.NameNotFoundException | RuntimeException error) {
            Log.w(TAG, "Unable to measure package signer for " + packageName, error);
            return "unknown";
        }
    }

    private static String digestUtf8(String value) {
        return digest(value.getBytes(java.nio.charset.StandardCharsets.UTF_8));
    }

    private static String digestUtf8Bounded(String value) {
        if (value == null) {
            return digestUtf8("");
        }
        final int prefixLength = Math.min(value.length(), 256);
        return digestUtf8(value.length() + ":" + value.substring(0, prefixLength));
    }

    private static String digest(byte[] value) {
        final MessageDigest digest = sSha256.get();
        digest.reset();
        return toHex(digest.digest(value));
    }

    private static long firstLong(String hexDigest) {
        long result = 0;
        final int length = Math.min(16, hexDigest.length());
        for (int index = 0; index < length; index++) {
            result = (result << 4) | Character.digit(hexDigest.charAt(index), 16);
        }
        return result;
    }

    private static long bindingToken(View target, Object callback) {
        return ((long) System.identityHashCode(target) << 32)
                | (System.identityHashCode(callback) & 0xffffffffL);
    }

    private static String toHex(byte[] bytes) {
        final char[] digits = "0123456789abcdef".toCharArray();
        final char[] result = new char[bytes.length * 2];
        for (int index = 0; index < bytes.length; index++) {
            final int value = bytes[index] & 0xff;
            result[index * 2] = digits[value >>> 4];
            result[index * 2 + 1] = digits[value & 0x0f];
        }
        return new String(result);
    }

    private static native int nativeResolveAndLog(Object callback, String methodName,
            String descriptor, String domain, int targetId, long targetToken, int inputEventId,
            int inputSource, String operation, String phase, String route, String argumentsHash);

    private static native long nativeBeginContinuation(Object callback, String methodName,
            String descriptor, Object bindingCallback, String bindingMethodName,
            String bindingDescriptor, String domain, int inputEventId, int inputSource,
            String operation, String route, String argumentsHash);

    private static native int nativeEndContinuation(long token);
}
