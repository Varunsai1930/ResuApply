"use strict";
document.addEventListener("click", function (event) {
  if (event.target.closest("[data-print-resume]")) window.print();
});
