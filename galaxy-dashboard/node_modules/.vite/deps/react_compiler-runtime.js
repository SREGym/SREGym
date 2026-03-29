import {
  __commonJS,
  require_react
} from "./chunk-BSNCPAQV.js";

// node_modules/react/cjs/react-compiler-runtime.development.js
var require_react_compiler_runtime_development = __commonJS({
  "node_modules/react/cjs/react-compiler-runtime.development.js"(exports) {
    "use strict";
    (function() {
      var ReactSharedInternals = require_react().__CLIENT_INTERNALS_DO_NOT_USE_OR_WARN_USERS_THEY_CANNOT_UPGRADE;
      exports.c = function(size) {
        var dispatcher = ReactSharedInternals.H;
        null === dispatcher && console.error(
          "Invalid hook call. Hooks can only be called inside of the body of a function component. This could happen for one of the following reasons:\n1. You might have mismatching versions of React and the renderer (such as React DOM)\n2. You might be breaking the Rules of Hooks\n3. You might have more than one copy of React in the same app\nSee https://react.dev/link/invalid-hook-call for tips about how to debug and fix this problem."
        );
        return dispatcher.useMemoCache(size);
      };
    })();
  }
});

// node_modules/react/compiler-runtime.js
var require_compiler_runtime = __commonJS({
  "node_modules/react/compiler-runtime.js"(exports, module) {
    if (false) {
      module.exports = null;
    } else {
      module.exports = require_react_compiler_runtime_development();
    }
  }
});
export default require_compiler_runtime();
//# sourceMappingURL=react_compiler-runtime.js.map
