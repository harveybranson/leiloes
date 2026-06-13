(function($) {
    "use strict";
  
    const $documentOn = $(document);
    const $windowOn = $(window);
  
    $documentOn.ready( function() {
  
      /* ================================
       Mobile Menu Js Start
    ================================ */
    
      $('#mobile-menu').meanmenu({
        meanMenuContainer: '.mobile-menu',
        meanScreenWidth: "1199",
        meanExpand: ['<i class="far fa-plus"></i>'],
    });

       $('#mobile-menus').meanmenu({
        meanMenuContainer: '.mobile-menus',
        meanScreenWidth: "19920",
        meanExpand: ['<i class="far fa-plus"></i>'],
    });

     $documentOn.on("click", ".mean-expand", function () {
        let icon = $(this).find("i");

        if (icon.hasClass("fa-plus")) {
            icon.removeClass("fa-plus").addClass("fa-minus"); 
        } else {
            icon.removeClass("fa-minus").addClass("fa-plus"); 
        }
    });

    /* ================================
        Sidebar Toggle & Sticky Item Logic
        ================================ */

        // Open offcanvas
        $(".sidebar__toggle").on("click", function () {
        $(".offcanvas__info").addClass("info-open");
        $(".offcanvas__overlay").addClass("overlay-open");

        // Hide sticky item
        $(".sidebar-sticky-item").fadeOut().removeClass("active");
        });

        // Close offcanvas
        $(".offcanvas__close, .offcanvas__overlay").on("click", function () {
        $(".offcanvas__info").removeClass("info-open");
        $(".offcanvas__overlay").removeClass("overlay-open");

        // Show sticky item
        $(".sidebar-sticky-item").fadeIn().addClass("active");
        });

        /* ================================
        Body Overlay Js Start
        ================================ */

        $(".body-overlay").on("click", function () {
        $(".offcanvas__area").removeClass("offcanvas-opened");
        $(".df-search-area").removeClass("opened");
        $(".body-overlay").removeClass("opened");

        // Show sticky item when overlay clicked
        $(".sidebar-sticky-item").fadeIn().addClass("active");
        });

        /* ================================
        Offcanvas Link Click (Optional)
        ================================ */

        $(".offcanvas a").on("click", function () {
        $(".sidebar-sticky-item").fadeIn().addClass("active");
    });

    
      /* ================================
       Sticky Header Js Start
    ================================ */

       $windowOn.on("scroll", function () {
        if ($(this).scrollTop() > 250) {
          $("#header-sticky").addClass("sticky");
        } else {
          $("#header-sticky").removeClass("sticky");
        }
      });      


      ////////////////////////////////////////////////////
	// 05. Search Js
	$(".search_btn").on("click", function () {
		$(".search_popup").addClass("search-opened");
		$(".search-popup-overlay").addClass("search-popup-overlay-open");
		$("body").addClass("overflow-hidden");
	});

	$(".search_close_btn").on("click", function () {
		$(".search_popup").removeClass("search-opened");
		$(".search-popup-overlay").removeClass("search-popup-overlay-open");
		$("body").removeClass("overflow-hidden");
	});
	$(".search-popup-overlay").on("click", function () {
		$(".search_popup").removeClass("search-opened");
		$(this).removeClass("search-popup-overlay-open");
		$("body").removeClass("overflow-hidden");
	});

      
       /* ================================
       Video & Image Popup Js Start
    ================================ */

      $(".img-popup").magnificPopup({
        type: "image",
        gallery: {
          enabled: true,
        },
      });

      $(".video-popup").magnificPopup({
        type: "iframe",
        callbacks: {},
      });
  
      /* ================================
       Counterup Js Start
    ================================ */

      $(".count").counterUp({
        delay: 15,
        time: 4000,
      });
  
      /* ================================
       Wow Animation Js Start
    ================================ */

      new WOW().init();
  
      /* ================================
       Nice Select Js Start
    ================================ */

    if ($('.single-select').length) {
        $('.single-select').niceSelect();
    }

      /* ================================
       Parallaxie Js Start
    ================================ */

      if ($('.parallaxie').length && $(window).width() > 991) {
          if ($(window).width() > 768) {
              $('.parallaxie').parallaxie({
                  speed: 0.55,
                  offset: 0,
              });
          }
      }

      /* ================================
      Hover Active Js Start
    ================================ */

    $(".counter-box, .service-card-item, .choose-list li, .feature-box-style-3, .about-wrapper-5 .about-icon-item, .service-box-style-5, .counter-box-style-5, .work-process-box-style-4, .contact-info-box").hover(
		// Function to run when the mouse enters the element
		function () {
			// Remove the "active" class from all elements
			$(".counter-box, .service-card-item, .choose-list li, .feature-box-style-3, .about-wrapper-5 .about-icon-item, .service-box-style-5, .counter-box-style-5, .work-process-box-style-4, .contact-info-box").removeClass("active");
			// Add the "active" class to the currently hovered element
			$(this).addClass("active");
		}
	);

    

    /* ================================
     Scrolldown Js Start
    ================================ */
    $("#scrollDown").on("click", function () {
        setTimeout(function () {
            $("html, body").animate({ scrollTop: "+=1000px" }, "slow");
        }, 1000);
    });

    /* ================================
    Image-Slider Js Start
    ================================ */
    if ($(".imgSlider").length > 0) {
    var swiper = new Swiper(".imgSlider", {
        spaceBetween: 24,
        slidesPerView: 4,
        freeMode: true,
        watchSlidesProgress: true,
    });
    }

    if ($(".imgSlider2").length > 0) {
      var swiper2 = new Swiper(".imgSlider2", {
        spaceBetween: 10,
        thumbs: {
          swiper: swiper,
        },
      });
    }

    /* ================================
      Brand Slider Js Start
    ================================ */

   if ($('.brand-slider').length > 0) {
    const brandSlider = new Swiper(".brand-slider", {
        spaceBetween: 30,
        speed: 1300,
        loop: true,
        autoplay: {
            delay: 2000,
            disableOnInteraction: false,
        },
        navigation: {
            nextEl: ".array-next",
            prevEl: ".array-prev",
        },
        breakpoints: {
            1399: {
                slidesPerView: 6,
            },
            1199: {
                slidesPerView: 5.5,
            },
            991: {
                slidesPerView: 4.5,
            },
            767: {
                slidesPerView: 3.3,
            },
            575: {
                slidesPerView: 2,
            },
            0: {
                slidesPerView: 1.3,
            },
        },
    });
   }


    //>> Project Slider Start <<//
      if($('.project-slider').length > 0) {
        const ProjectSlider = new Swiper(".project-slider", {
            spaceBetween: 20,
            speed: 1300,
            loop: true,
             centeredSlides: true,
            autoplay: {
                delay: 2000,
                disableOnInteraction: false,
            },
           navigation: {
                prevEl: ".array-prev",
                nextEl: ".array-next",
            },
            breakpoints: {
                 1399: {
                    slidesPerView: 5,
                },
                1199: {
                    slidesPerView: 3.1,
                },
                991: {
                    slidesPerView: 2.1,
                },
                767: {
                    slidesPerView: 2.1,
                },
                575: {
                    slidesPerView: 2.2,
                },
                0: {
                    slidesPerView: 1.2,
                },
            },
        });
      }

    //>> Hero Slider Start <<//
      if($('.hero-slider').length > 0) {
        const HeroSlider = new Swiper(".hero-slider", {
            spaceBetween: 20,
            speed: 800,
            loop: true,
             effect: "slide",
            autoplay: {
                delay: 4000,
                disableOnInteraction: false,
            },
             
           pagination: {
                el: ".dot",
                clickable: true,
            },
            breakpoints: {
                1199: {
                    slidesPerView: 4,
                },
                991: {
                    slidesPerView: 4,
                },
                767: {
                    slidesPerView: 3,
                },
                575: {
                    slidesPerView: 2,
                },
                0: {
                    slidesPerView: 2,
                },
            },
        });
      }

      
      //>> Image Slider Start <<//
      var swiper = new Swiper(".image-slider", {
        loop: true,
        autoplay: {
            delay: 4000,
            disableOnInteraction: false,
        },
        speed: 800,
        effect: "slide",
        pagination: {
                el: ".dot",
                clickable: true,
            },
      on: {
        slideChangeTransitionStart: function () {
          document.querySelectorAll('.hero-image img').forEach(img => {
            img.classList.remove('animate__fadeInUp'); 
          });
        },
        slideChangeTransitionEnd: function () {
          let activeImg = document.querySelector('.swiper-slide-active .hero-image img');
          if(activeImg){
            activeImg.classList.add('animate__animated', 'animate__fadeInUp');
          }
        }
      }
      });

       /* ================================
      Testimonial Slider Js Start
    ================================ */

   if ($('.testimonial-slider').length > 0) {
    const TestimonialSlider4 = new Swiper(".testimonial-slider", {
        spaceBetween: 30,
        speed: 1300,
        loop: true,
        autoplay: {
            delay: 2000,
            disableOnInteraction: false,
        },
        navigation: {
            nextEl: ".array-next",
            prevEl: ".array-prev",
        },
        pagination: {
            el: ".dot",
            clickable: true,
        },
        breakpoints: {
             1399: {
                slidesPerView: 4,
            },
            1199: {
                slidesPerView: 3,
            },
            991: {
                slidesPerView: 2,
            },
            767: {
                slidesPerView: 2,
            },
            575: {
                slidesPerView: 1,
            },
            0: {
                slidesPerView: 1,
            },
        },
    });
   }

   /* ================================
       Propertie Slider Js Start
    ================================ */

    if($('.propertie-slider1').length > 0) {
        const propertieSlider1 = new Swiper(".propertie-slider1", {
            spaceBetween: 30,
            speed: 1300,
            loop: true,
            autoplay: {
                delay: 2000,
                disableOnInteraction: false,
            },
            navigation: {
                nextEl: ".array-prev",
                prevEl: ".array-next",
            },
            breakpoints: {
                1199: {
                    slidesPerView: 3,
                },
                991: {
                    slidesPerView: 2,
                },
                767: {
                    slidesPerView: 2,
                },
                575: {
                    slidesPerView: 1,
                },
                400: {
                    slidesPerView: 1,
                },
            },
        });
    }

    if ($('.propertie-slider2').length > 0) {
      const propertieSlider2 = new Swiper(".propertie-slider2", {
          spaceBetween: 30,
          speed: 1300,
          loop: true,
          autoplay: {
              delay: 2000,
              disableOnInteraction: false,
              reverseDirection: true, // 👉 scrolls to the right
          },
          navigation: {
              nextEl: ".array-prev",
              prevEl: ".array-next",
          },
          breakpoints: {
              1199: {
                  slidesPerView: 4,
              },
              991: {
                  slidesPerView: 2.8,
              },
              767: {
                  slidesPerView: 2,
              },
              575: {
                  slidesPerView: 1.2,
              },
              400: {
                  slidesPerView: 1,
              },
          },
      });
    }
    
    if($('.propertie-slider3').length > 0) {
        const propertieSlider3 = new Swiper(".propertie-slider3", {
            spaceBetween: 30,
            speed: 1300,
            loop: true,
            centeredSlides: true,
            autoplay: {
                delay: 2000,
                disableOnInteraction: false,
            },
            navigation: {
                nextEl: ".array-prev",
                prevEl: ".array-next",
            },
            breakpoints: {
                1199: {
                    slidesPerView: 3,
                },
                991: {
                    slidesPerView: 2,
                },
                767: {
                    slidesPerView: 2,
                },
                575: {
                    slidesPerView: 1,
                },
                400: {
                    slidesPerView: 1,
                },
            },
        });
    }
    
    /* ================================
       GT Team Slider Js Start
    ================================ */
    if($('.team-slider').length > 0) {
        const TeamSlider = new Swiper(".team-slider", {
          effect: "coverflow",
          spaceBetween: 60,
          autoplay: true,
          centeredSlides: true,
          loop: true,
          autoplay: {
            delay: 2000,
            disableOnInteraction: false,
          },
          coverflowEffect: {
              rotate: 50,
              stretch: 0,
              depth: 100,
              modifier: 1,
              slideShadows: false,
              scale: 1
          },

          navigation: {
                nextEl: ".array-prev",
                prevEl: ".array-next",
            },

          breakpoints: {
                1399: {
                    slidesPerView: 3,
                     spaceBetween: 60,
                },
                 1199: {
                    slidesPerView: 3,
                    spaceBetween: 30,
                },
                991: {
                    slidesPerView: 2,
                    spaceBetween: 30,
                },
                767: {
                    slidesPerView: 2,
                    spaceBetween: 30,
                },
                575: {
                    slidesPerView: 1,
                    spaceBetween: 30,
                },
                0: {
                    slidesPerView: 1,
                },
          },
        });
    }

    /* ================================
       Testimonial Slider Js Start
    ================================ */
    const getSlide = $('listing-wrapper, .listing-items-thumb').length - 1;
    const slideCal = 100 / getSlide + '%';
    
    $('.listing-items-thumb').css({
        "width": slideCal
    });
    
    $(document).on('mouseenter', '.listing-items-thumb', function() {
        $('.listing-items-thumb').removeClass('active');
        $(this).addClass('active');
    }); 

    /* ================================
      Team ACTIVE Js Start
    ================================ */
    const getSlide2 = $('gt-team-wrapper, .team-box-img-2').length - 1;
    const slideCal2 = 100 / getSlide2 + '%';
    
    $('.gt-team-wrapper').css({
        "width": slideCal2
    });
    
    $(document).on('mouseenter', '.team-box-img-2', function() {
        $('.team-box-img-2').removeClass('active');
        $(this).addClass('active');
    });     


     if($('.testi-box-slider').length > 0) {
        const testiBoxSlider = new Swiper(".testi-box-slider", {
            spaceBetween: 30,
            speed: 1300,
            loop: true,
            autoplay: {
                delay: 2000,
                disableOnInteraction: false,
            },
        });
    }

    if($('.testi-image-slider').length > 0) {
        const testiImageSlider = new Swiper(".testi-image-slider", {
            spaceBetween: 30,
            speed: 1300,
            loop: true,
            autoplay: {
                delay: 2000,
                disableOnInteraction: false,
            },
             breakpoints: {
              991: {
                  slidesPerView: 4,
              },
              767: {
                  slidesPerView: 3,
              },
              575: {
                  slidesPerView: 3,
              },
              400: {
                  slidesPerView: 1,
              },
          },
        });
    }
    
    /* ================================
      Custom Accordion Js Start
    ================================ */

   if ($('.accordion-box').length) {
        $(".accordion-box").on('click', '.acc-btn', function () {
            var outerBox = $(this).closest('.accordion-box');
            var target = $(this).closest('.accordion');
            var accBtn = $(this);
            var accContent = accBtn.next('.acc-content');

            if (target.hasClass('active-block')) {
                // Already open, so close it
                accBtn.removeClass('active');
                target.removeClass('active-block');
                accContent.slideUp(300);
            } else {
                // Close all others
                outerBox.find('.accordion').removeClass('active-block');
                outerBox.find('.acc-btn').removeClass('active');
                outerBox.find('.acc-content').slideUp(300);

                // Open clicked one
                accBtn.addClass('active');
                target.addClass('active-block');
                accContent.slideDown(300);
            }
        });
    }

    /* ================================
        Mouse Cursor Animation Js Start
    ================================ */

    if ($(".mouseCursor").length > 0) {
        function itCursor() {
            var myCursor = jQuery(".mouseCursor");
            if (myCursor.length) {
                if ($("body")) {
                    const e = document.querySelector(".cursor-inner"),
                        t = document.querySelector(".cursor-outer");
                    let n, i = 0, o = !1;
                    window.onmousemove = function(s) {
                        if (!o) {
                            t.style.transform = "translate(" + s.clientX + "px, " + s.clientY + "px)";
                        }
                        e.style.transform = "translate(" + s.clientX + "px, " + s.clientY + "px)";
                        n = s.clientY;
                        i = s.clientX;
                    };
                    $("body").on("mouseenter", "button, a, .cursor-pointer", function() {
                        e.classList.add("cursor-hover");
                        t.classList.add("cursor-hover");
                    });
                    $("body").on("mouseleave", "button, a, .cursor-pointer", function() {
                        if (!($(this).is("a", "button") && $(this).closest(".cursor-pointer").length)) {
                            e.classList.remove("cursor-hover");
                            t.classList.remove("cursor-hover");
                        }
                    });
                    e.style.visibility = "visible";
                    t.style.visibility = "visible";
                }
            }
        }
        itCursor();
    }

    /* ================================
        Back To Top Button Js Start
    ================================ */
    $windowOn.on('scroll', function() {
        var windowScrollTop = $(this).scrollTop();
        var windowHeight = $(window).height();
        var documentHeight = $(document).height();

        if (windowScrollTop + windowHeight >= documentHeight - 10) {
            $("#back-top").addClass("show");
        } else {
            $("#back-top").removeClass("show");
        }
    });

    $documentOn.on('click', '#back-top', function() {
        $('html, body').animate({ scrollTop: 0 }, 800);
        return false;
    });

    /* ================================
       Search Popup Toggle Js Start
    ================================ */

    // if ($(".search-toggler").length) {
    //     $(".search-toggler").on("click", function(e) {
    //         e.preventDefault();
    //         $(".search-popup").toggleClass("active");
    //         $("body").toggleClass("locked");
    //     });
    // }
	
    /* ================================
       Smooth Scroller And Title Animation Js Start
    ================================ */
    if ($('#smooth-wrapper').length && $('#smooth-content').length) {
        gsap.registerPlugin(ScrollTrigger, ScrollSmoother, SplitText);

        gsap.config({
            nullTargetWarn: false,
        });

        let smoother = ScrollSmoother.create({
            wrapper: "#smooth-wrapper",
            content: "#smooth-content",
            smooth: 2,
            effects: true,
            smoothTouch: 0.1,
            normalizeScroll: false,
            ignoreMobileResize: true,
        });
    }

   

  /* ==================================================
    Image Scale
    ================================================== */
   var width = $(window).width();

    if (width > 1023) {
        if (document.querySelectorAll(".scale-animation").length > 0) {

            gsap.registerPlugin(ScrollTrigger);

            gsap.utils.toArray(".scale-animation").forEach(function (section) {

                gsap.timeline({
                    scrollTrigger: {
                        trigger: section,
                        scrub: 3,
                        start: "top 90%",
                        end: "bottom 70%",
                    },
                })
                .from(section, {
                    scale: 0.8,
                    opacity: 0,
                    transformOrigin: "center bottom",
                    duration: 1.5,
                    ease: "power2.out",
                })
                .to(section, {
                    scale: 1,
                    opacity: 1,
                    transformOrigin: "center bottom",
                    duration: 1.2,
                    ease: "power2.out",
                });
            });
        }
    }

  if ($('.full-img-wrap3').length > 0) {
        // Check window width
        if (window.innerWidth > 1399) {
            ScrollTrigger.create({
                trigger: ".full-img-wrap3",
                start: "top 0",
                end: "bottom 0%",
                pin: ".full-img3",
                pinSpacing: false,
            });
        }
    }


    // ScrollTrigger register করতে ভুলবেন না
    gsap.registerPlugin(ScrollTrigger);

    gsap.utils.toArray(".tp_fade_anim").forEach((item) => {
        let tp_fade_offset = item.getAttribute("data-fade-offset") || 40,
            tp_duration_value = item.getAttribute("data-duration") || 0.75,
            tp_fade_direction = item.getAttribute("data-fade-from") || "bottom",
            tp_onscroll_value = item.getAttribute("data-on-scroll") || 1,
            tp_delay_value = item.getAttribute("data-delay") || 0.15,
            tp_ease_value = item.getAttribute("data-ease") || "power2.out";

        let tp_anim_setting = {
            opacity: 0,
            ease: tp_ease_value,
            duration: tp_duration_value,
            delay: tp_delay_value,
            x: (tp_fade_direction == "left" ? -tp_fade_offset : (tp_fade_direction == "right" ? tp_fade_offset : 0)),
            y: (tp_fade_direction == "top" ? -tp_fade_offset : (tp_fade_direction == "bottom" ? tp_fade_offset : 0)),
        };

        // Scroll এ animate হবে
        if (tp_onscroll_value == 1) {
            tp_anim_setting.scrollTrigger = {
                trigger: item,
                start: "top 85%",
                toggleActions: "play none none reset",
            };
        }

        gsap.from(item, tp_anim_setting);
    });

   
      // rr_title_anim 
    if (document.querySelectorAll(".animation").length > 0) {
    let animations = document.querySelectorAll(".animation");

    animations.forEach((animation) => {
        const title = animation.querySelector(".gt_title_animation");
        const sup = animation.querySelector(".sup_animation");

        if (!title) return;

        const split = new SplitText(title, { type: "chars, words" });

        let tl = gsap.timeline({ paused: true });

        if (sup) {
        tl.to(sup, { opacity: 1, x: -50, ease: "back" });
        }

        tl.from(split.chars, {
        opacity: 0,
        y: 50,
        rotation: 1,
        duration: 2,
        ease: "back",
        stagger: 0.05,
        });

        ScrollTrigger.create({
        trigger: animation,
        start: "top bottom",
        toggleActions: "play none none reverse",
        onEnter: () => tl.timeScale(2.3).play(),
        onLeaveBack: () => tl.timeScale(2.3).reverse(),
        });
    });
    }


  // rr-char-animation
  if (
    document.querySelectorAll(".gt-char-animation").length > 0 &&
    window.innerWidth > 768
  ) {
    let char_come = gsap.utils.toArray(".gt-char-animation");
    char_come.forEach((splitTextLine) => {
      const tl = gsap.timeline({
        scrollTrigger: {
          trigger: splitTextLine,
          start: "top 90%",
          end: "bottom 5%",
          scrub: false,
          markers: false,
          toggleActions: "play none none reverse",
        },
      });

      const itemSplitted = new SplitText(splitTextLine, {
        type: "chars, words",
      });
      gsap.set(splitTextLine, { perspective: 300 });

      itemSplitted.split({ type: "chars, words" });

      tl.from(itemSplitted.chars, {
        duration: 0.4,
        delay: 0.1,
        x: 100,
        autoAlpha: 0,
        stagger: 0.05,
      });
    });
  }

        // GSAP title animation
    if (document.querySelectorAll(".gt_title_anim").length > 0) {
        if ($('.gt_title_anim').length > 0) {
        let splitTitleLines = gsap.utils.toArray(".gt_title_anim");
        splitTitleLines.forEach(splitTextLine => {
            const tl = gsap.timeline({
            scrollTrigger: {
                trigger: splitTextLine,
                start: 'top 90%',
                end: 'bottom 60%',
                scrub: false,
                markers: false,
                toggleActions: 'play none none reverse'
            }
            });

            const itemSplitted = new SplitText(splitTextLine, { type: "words, lines" });
            gsap.set(splitTextLine, { perspective: 400 });
            itemSplitted.split({ type: "lines" })
            tl.from(itemSplitted.lines, {
            duration: 1,
            delay: 0.3,
            opacity: 0,
            rotationX: -80,
            force3D: true,
            transformOrigin: "top center -50",
            stagger: 0.1
            });
        });
        }
    }


        // Project-card-wrapper-4 animation 
        gsap.utils.toArray(".project-card-wrapper-4 .project-card-items-4").forEach((element, index, array) => {
        if (index === array.length - 1) return;

            const delay = parseFloat(element.getAttribute("data-ani-delay")) || 0;
            gsap.to(element, {
                scale: .6,
                opacity: 0,
                duration: 2,
                delay: delay,
                scrollTrigger: {
                    trigger: element,
                    start: "top 15%",
                    end: "bottom 15%",
                    scrub: 2,
                    pin: true,
                    pinSpacing: false,
                    markers: false
                }
            });
        });


        // 18. webgl images hover animation //
    if ($('.gt--hover-item').length) {
        let hoverAnimation__do = function (t, n) {
            let a = new hoverEffect({
                parent: t.get(0),
                intensity: t.data("intensity") || void 0,
                speedIn: t.data("speedin") || void 0,
                speedOut: t.data("speedout") || void 0,
                easing: t.data("easing") || void 0,
                hover: t.data("hover") || void 0,
                image1: n.eq(0).attr("src"),
                image2: n.eq(0).attr("src"),
                displacementImage: t.data("displacement"),
                imagesRatio: n[0].height / n[0].width,
                hover: !1
            });
            t.closest(".gt--hover-item").on("mouseenter", function () {
                a.next()
            }).on("mouseleave", function () {
                a.previous()
            })
        }
        let hoverAnimation = function () {
            $(".gt--hover-img").each(function () {
                let n = $(this);
                let e = n.find("img");
                let i = e.eq(0);
                i[0].complete ? hoverAnimation__do(n, e) : i.on("load", function () {
                    hoverAnimation__do(n, e)
                })
            })
        }
        hoverAnimation();
    }

    
    
    }); // End Document Ready Function

    $(document).ready(function() {
    // Make sure GSAP & Matter.js loaded
    $("[data-t-throwable-scene]").tThrowable();
    });

     //Price Range Slideer
    document.addEventListener("DOMContentLoaded", function () {
        const minSlider = document.getElementById("min-slider");
        const maxSlider = document.getElementById("max-slider");
        const amount = document.getElementById("amount");

        function updateAmount() {
            const minValue = parseInt(minSlider.value, 10);
            const maxValue = parseInt(maxSlider.value, 10);

            // Ensure the minimum value is always lower than the maximum value
            if (minValue > maxValue) {
                minSlider.value = maxValue;
            }

            // Update the displayed price range
            amount.value = "$" + minSlider.value + " - $" + maxSlider.value;

            // Calculate the percentage positions of the sliders
            const minPercent =
                ((minSlider.value - minSlider.min) /
                    (minSlider.max - minSlider.min)) *
                100;
            const maxPercent =
                ((maxSlider.value - maxSlider.min) /
                    (maxSlider.max - maxSlider.min)) *
                100;

            // Update the background gradient to show the active track color
            minSlider.style.background = `linear-gradient(to right, #000 ${minPercent}%, #E3572D ${minPercent}%, #E3572D ${maxPercent}%, #000 ${maxPercent}%)`;
            maxSlider.style.background = `linear-gradient(to right, #000 ${minPercent}%, #E3572D ${minPercent}%, #E3572D ${maxPercent}%, #000 ${maxPercent}%)`;
        }

        // Initialize the sliders and track with default values
        amount && updateAmount();

        // if (minSlider && maxSlider) {

        // Add event listeners for both sliders
        minSlider && minSlider.addEventListener("input", updateAmount);
        maxSlider && maxSlider.addEventListener("input", updateAmount);
        // }
    });
     /* ================================
       Preloader Js Start
    ================================ */

     function loader() {
        $(window).on('load', function() {
            // Animate loader off screen
            $(".preloader").addClass('loaded');                    
            $(".preloader").delay(600).fadeOut();                       
        });
    }
    loader();

    
  })(jQuery); // End jQuery

