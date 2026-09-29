;;; mpv-vui.el --- A vui status line for mpv  -*- lexical-binding: t; -*-

;; Copyright (C) 2026  Qiqi Jin

;; Author: Qiqi Jin <ginqi7@gmail.com>
;; Version: 1.0
;; URL: https://github.com/ginqi7/mpv.el
;; Keywords: multimedia, video

;; This program is free software; you can redistribute it and/or modify
;; it under the terms of the GNU General Public License as published by
;; the Free Software Foundation, either version 3 of the License, or
;; (at your option) any later version.

;; This program is distributed in the hope that it will be useful,
;; but WITHOUT ANY WARRANTY; without even the implied warranty of
;; MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
;; GNU General Public License for more details.

;; You should have received a copy of the GNU General Public License
;; along with this program.  If not, see <https://www.gnu.org/licenses/>.

;;; Commentary:

;; A vui component showing the file mpv is playing and how far along it
;; is.  It subscribes to `mpv--time-update-functions', so mounting it
;; once is enough: `mpv-vui-render' updates it from then on.

;;; Code:

(require 'mpv)
(require 'vui)

(defvar mpv-vui--component nil
  "The mounted `mpv-vui' instance, or nil before the first render.

Kept so that later renders update the existing component instead of
mounting a second one.")

(vui-defcomponent mpv-vui-progress (total done)
  :render
  (let* ((width 40)
         (done-width (if (> total 0)
                         (round (* width (/ done total)))
                       0))
         (done-width (max 0 (min width done-width))))
    (vui-hstack
     (vui-text (concat "[" (make-string done-width ?#)
                       (make-string (- width done-width) ?-) "]")
       :face 'success)
     (vui-text (format "(%s / %s)" done total)))))

(vui-defcomponent mpv-vui-video-info ()
  :render
  (vui-table
   :columns '((:header "Key") (:header "Value"))
   :rows (mapcar (lambda (key)
                   (list key (format "%s" (plist-get mpv--video-info key #'equal))))
                 '("title" "duration"))))

(vui-defcomponent mpv-vui (done)
  :render
  (vui-vstack
   (vui-component 'mpv-vui-video-info)
   (vui-component 'mpv-vui-progress
                  :total (plist-get mpv--video-info "duration" #'equal)
                  :done done)))

(defun mpv-vui-render (time)
  "Update the mpv status component to playback position TIME.

Mounts the component on the first call and reuses it afterwards."
  (if mpv-vui--component
      (vui-update-props mpv-vui--component
                        (list :done time))
      (setq mpv-vui--component
            (vui-mount (vui-component 'mpv-vui :done time) "*mpv-vui*"))))

(add-to-list 'mpv--time-update-functions #'mpv-vui-render)

;; (vui-mount (vui-component 'mpv-vui :done 400) "*mpv-vui*")

(provide 'mpv-vui)
;;; mpv-vui.el ends here
